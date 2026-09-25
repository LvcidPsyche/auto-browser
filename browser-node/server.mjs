import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, readFile, readdir, rename, rm, unlink, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { connect as netConnect } from "node:net";
import { hostname } from "node:os";
import { dirname, join } from "node:path";
import { chromium } from "playwright";

const width = Number.parseInt(process.env.BROWSER_WIDTH || "1280", 10);
const height = Number.parseInt(process.env.BROWSER_HEIGHT || "800", 10);
const endpointFile = process.env.BROWSER_WS_ENDPOINT_FILE || "/data/profile/browser-ws-endpoint.txt";
const host = process.env.PLAYWRIGHT_SERVER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.PLAYWRIGHT_SERVER_PORT || "9223", 10);
const advertisedHost = process.env.PLAYWRIGHT_SERVER_ADVERTISED_HOST || "browser-node";
const downloadsDir = process.env.BROWSER_DOWNLOADS_DIR || "/data/downloads";

// Persistent Chromium profiles -- one named identity's on-disk user-data-dir
// (owner-default, nihad-google, ...), launched on demand, so IndexedDB,
// service workers, cache and history survive across Opens, controller
// restarts and image rebuilds. See controller/app/persistent_profiles.py for
// the client side of this protocol. Unset (the code default), this whole
// feature is inert and the container behaves exactly as before.
const persistentProfilesEnabled = (process.env.PERSISTENT_PROFILES_ENABLED || "false").toLowerCase() === "true";
const profileControlHost = process.env.PROFILE_CONTROL_HOST || "0.0.0.0";
const profileControlPort = Number.parseInt(process.env.PROFILE_CONTROL_PORT || "9224", 10);
// Chromium binds --remote-debugging-port to 127.0.0.1 no matter what
// --remote-debugging-address says (that switch is honoured by headless only),
// so the controller -- another container -- can never reach a profile's CDP
// port directly. This relay is the only way in: it listens on the tenant
// network, requires the shared bearer token, and pipes to the loopback port.
const cdpRelayHost = process.env.PROFILE_CDP_RELAY_HOST || "0.0.0.0";
const cdpRelayPort = Number.parseInt(process.env.PROFILE_CDP_RELAY_PORT || "9225", 10);
const cdpRelayAdvertisedHost = process.env.PROFILE_CDP_RELAY_ADVERTISED_HOST || advertisedHost;
// Shared secret between browser-node and the controller. Required for every
// /profiles/* call and every relayed CDP connection; without it configured
// those endpoints refuse everything (fail closed), /healthz stays up.
const profileControlToken = process.env.PROFILE_CONTROL_TOKEN || "";
const profilesRoot = process.env.BROWSER_PROFILES_ROOT || "/data/browser-profiles";
// Never deleted automatically: delete/rename/import/owner-change move a
// profile here, so a login is never silently lost. Cleanup is manual.
const trashRoot = join(profilesRoot, ".trash");
const healthcheckRoot = join(profilesRoot, ".healthcheck");
// Node lease: which browser-node container currently owns this volume's
// profiles. Chromium's own profile locks name a hostname/pid that a /proc scan
// in THIS container cannot see if another container (another PID namespace)
// shares the volume, so lock recovery and launches are only done by the node
// holding a fresh lease. Heartbeat every 10s; another node's lease counts as
// alive until it is PROFILE_NODE_LEASE_STALE_SECONDS old.
const nodeLeaseFile = join(profilesRoot, ".node-lease.json");
const nodeId = `${hostname()}:${process.pid}:${randomBytes(4).toString("hex")}`;
const nodeLeaseHeartbeatMs = Number.parseFloat(process.env.PROFILE_NODE_LEASE_HEARTBEAT_SECONDS || "10") * 1000;
const nodeLeaseStaleMs = Number.parseFloat(process.env.PROFILE_NODE_LEASE_STALE_SECONDS || "45") * 1000;
// Manual override for a lease left by a node that is known to be gone (for
// example a crashed container whose volume is now mounted elsewhere). Takes
// the lease at startup regardless of its age. Never set it while another
// browser-node may really be running on the same volume.
const nodeLeaseForce = (process.env.PROFILE_NODE_LEASE_FORCE || "").toLowerCase() === "true";
const deepHealthTtlMs = Number.parseFloat(process.env.PROFILE_DEEP_HEALTH_TTL_SECONDS || "300") * 1000;
// A FAILED deep check is cached too. The container healthcheck polls every
// 10s; without this, a failing check launched a fresh disposable Chromium on
// every poll. Under the container's pid cap that loop is what starved the
// owner's live profile of threads (2026-09-25: 79 healthcheck Chromium
// crashes in 8 minutes, then the owner's own browser process crashed).
const deepHealthFailureTtlMs = Number.parseFloat(process.env.PROFILE_DEEP_HEALTH_FAILURE_TTL_SECONDS || "60") * 1000;
const defaultLocale = process.env.PERSISTENT_PROFILE_LOCALE || "ar-EG";
const defaultTimezoneId = process.env.PERSISTENT_PROFILE_TIMEZONE || "Africa/Cairo";

const PROFILE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
const OWNER_MARKER = ".auto-browser-owner.json";
// Chromium's own "this profile is in use" markers (Linux) plus the CDP port
// file. After a killed container they are left behind pointing at the old
// container's hostname/pid, and Chromium then refuses the profile as "in use
// by another computer". Removed only when no live process holds the profile.
const STALE_LOCK_FILES = ["SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// name -> { context, localPort, wsPath, cdpEndpoint, owner, generation }
const profiles = new Map();
// Every successful open (launch or re-attach) gets a new generation. A close
// must name the generation it was given; a close for an older generation --
// e.g. a slow close from a session that was replaced by a newer Open of the
// same profile -- is a no-op instead of killing the newer session's browser.
// `${bootId}-${n}`: unique across restarts too, so a close sent to a
// previous browser-node process can never match an open of this one.
const bootId = randomBytes(8).toString("hex");
let generationCounter = 0;
function nextGeneration() {
  generationCounter += 1;
  return `${bootId}-${generationCounter}`;
}
// relay id -> loopback CDP port. Profile names map to themselves; the deep
// healthcheck registers a temporary id that no profile name can collide with.
const relayTargets = new Map();

// Every lifecycle operation (open/close/trash/rename) runs one at a time.
// The controller holds one exclusive lease per profile, and this is a
// single-owner stack, so serializing is cheap and removes every race between
// "launch", "close" and "move the directory" on the same user-data-dir.
let lifecycleChain = Promise.resolve();
function withLifecycleLock(fn) {
  const run = lifecycleChain.then(fn, fn);
  lifecycleChain = run.catch(() => {});
  return run;
}

function tokenMatches(authorizationHeader) {
  if (!profileControlToken) return false;
  const match = /^Bearer\s+(.+)$/i.exec(authorizationHeader || "");
  if (!match) return false;
  // Hash both sides so timingSafeEqual always compares equal-length buffers
  // (it throws on a length mismatch, which would itself leak the length).
  const supplied = createHash("sha256").update(match[1].trim(), "utf8").digest();
  const expected = createHash("sha256").update(profileControlToken, "utf8").digest();
  return timingSafeEqual(supplied, expected);
}

function profileDir(name) {
  return join(profilesRoot, name);
}

function normalizeOwner(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * state: "missing" (no marker file), "ok" (owner read; null = unowned), or
 * "bad" (unreadable or malformed). A bad marker is never read as "unowned":
 * that would let anyone claim the directory's logins.
 */
async function readOwnerMarker(dir) {
  let raw;
  try {
    raw = await readFile(join(dir, OWNER_MARKER), "utf-8");
  } catch (err) {
    if (err && err.code === "ENOENT") return { state: "missing", owner: null };
    return { state: "bad", owner: null, error: err.message };
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || !("owner" in parsed)) {
      return { state: "bad", owner: null, error: "marker has no owner field" };
    }
    if (parsed.owner !== null && typeof parsed.owner !== "string") {
      return { state: "bad", owner: null, error: "marker owner is not a string" };
    }
    return { state: "ok", owner: normalizeOwner(parsed.owner) };
  } catch (err) {
    return { state: "bad", owner: null, error: err.message };
  }
}

/** Whether a directory holds anything beyond our own bookkeeping files. */
async function hasProfileData(dir) {
  try {
    const entries = await readdir(dir);
    return entries.some((entry) => entry !== OWNER_MARKER && !STALE_LOCK_FILES.includes(entry));
  } catch {
    return false;
  }
}

async function writeOwnerMarker(dir, owner) {
  const payload = JSON.stringify({ owner, updated_at: new Date().toISOString() });
  await writeFile(join(dir, OWNER_MARKER), payload, { encoding: "utf-8", mode: 0o600 });
}

/**
 * PIDs of any live process in THIS container whose command line names this
 * user-data-dir. Linux only. A /proc we cannot read proves nothing, so it
 * fails closed (throws) instead of reporting "no holder". Processes in other
 * containers are invisible here by design; the node lease covers them. On
 * non-Linux (local development) there is no /proc and only the node lease
 * guards the profile.
 */
async function profileHolderPids(dir) {
  if (process.platform !== "linux") return [];
  const needle = `--user-data-dir=${dir}`;
  const holders = [];
  let entries = [];
  try {
    entries = await readdir("/proc");
  } catch (err) {
    throw new HttpError(503, `cannot read /proc to check who holds ${dir}: ${err.message}`);
  }
  for (const entry of entries) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const cmdline = await readFile(`/proc/${entry}/cmdline`, "utf-8");
      if (cmdline.split("\0").includes(needle)) holders.push(Number(entry));
    } catch (err) {
      // Gone while we looked: not a holder. Anything else (EACCES, EIO...)
      // proves nothing, so it counts as a possible holder (fail closed).
      if (!err || (err.code !== "ENOENT" && err.code !== "ESRCH")) holders.push(Number(entry));
    }
  }
  return holders;
}

let nodeLeaseHeld = false;
let nodeLeaseBlockedBy = null;

/**
 * The lease's age comes from the file's mtime (every heartbeat rewrites the
 * file), capped by a valid heartbeat_at inside it. So a malformed, garbled or
 * future-dated lease is still treated as foreign and fresh at first (fail
 * closed) but goes stale like any other once nobody rewrites it -- it can
 * never lock the owner out for good.
 */
async function readNodeLease() {
  let mtimeMs;
  try {
    mtimeMs = (await lstat(nodeLeaseFile)).mtimeMs;
  } catch (err) {
    if (err && err.code === "ENOENT") return null;
    return { node_id: "<unreadable>", heartbeat_at: Date.now() };
  }
  let parsed = null;
  try {
    parsed = JSON.parse(await readFile(nodeLeaseFile, "utf-8"));
  } catch (err) {
    if (err && err.code === "ENOENT") return null;
  }
  const nodeIdValue = parsed && typeof parsed.node_id === "string" && parsed.node_id ? parsed.node_id : "<malformed>";
  const heartbeat =
    parsed && Number.isFinite(parsed.heartbeat_at) ? Math.min(parsed.heartbeat_at, mtimeMs) : mtimeMs;
  return { node_id: nodeIdValue, heartbeat_at: heartbeat };
}

async function writeNodeLease() {
  await mkdir(profilesRoot, { recursive: true, mode: 0o700 });
  const tmp = `${nodeLeaseFile}.${randomBytes(4).toString("hex")}.tmp`;
  await writeFile(tmp, JSON.stringify({ node_id: nodeId, heartbeat_at: Date.now() }), { encoding: "utf-8", mode: 0o600 });
  await rename(tmp, nodeLeaseFile);
}

/**
 * Take or keep the node lease. Returns true when this node holds it. Another
 * node's lease that is younger than the stale window blocks us (fail closed)
 * until it stops heart-beating, unless PROFILE_NODE_LEASE_FORCE=true.
 */
async function refreshNodeLease({ force = false } = {}) {
  const current = await readNodeLease();
  const foreignAndFresh =
    current && current.node_id !== nodeId && Date.now() - current.heartbeat_at < nodeLeaseStaleMs;
  if (foreignAndFresh && !force) {
    if (nodeLeaseHeld || profiles.size) {
      // We lost the volume to another node: stop touching it at once.
      await fenceSelf(`another browser-node (${current.node_id}) now holds the lease`);
    }
    if (nodeLeaseHeld || nodeLeaseBlockedBy !== current.node_id) {
      console.error(
        `node lease: another browser-node (${current.node_id}) heart-beat ` +
          `${Math.round((Date.now() - current.heartbeat_at) / 1000)}s ago on this volume -- refusing to ` +
          "launch or unlock any profile until it stops (PROFILE_NODE_LEASE_FORCE=true overrides)",
      );
    }
    nodeLeaseHeld = false;
    nodeLeaseBlockedBy = current.node_id;
    return false;
  }
  await writeNodeLease();
  // Two nodes starting together could both write; the loser sees the other's
  // id on the re-read and backs off.
  await sleep(200);
  const check = await readNodeLease();
  const held = Boolean(check && check.node_id === nodeId);
  if (held && !nodeLeaseHeld) {
    console.log(`node lease acquired by ${nodeId}${current && current.node_id !== nodeId ? ` (previous: ${current.node_id})` : ""}`);
  }
  nodeLeaseHeld = held;
  nodeLeaseBlockedBy = held ? null : check && check.node_id;
  return held;
}

// relay id -> live relayed sockets, so fencing can cut every connection.
const relaySockets = new Map();

/**
 * Called when another node holds the lease: close every persistent profile
 * this node runs and cut every relayed CDP connection. Opens and relay
 * connections are refused afterwards because each one re-checks the lease.
 */
async function fenceSelf(reason) {
  console.error(`node lease lost (${reason}): fencing -- closing ${profiles.size} profile(s), cutting relay connections`);
  nodeLeaseHeld = false;
  for (const [id, sockets] of relaySockets) {
    for (const sock of sockets) sock.destroy();
    relaySockets.delete(id);
  }
  const entries = [...profiles.entries()];
  profiles.clear();
  for (const [name] of entries) relayTargets.delete(name);
  await Promise.all(
    entries.map(([name, entry]) =>
      entry.context.close().catch((err) => console.error(`fencing: close of '${name}' failed: ${err.message}`)),
    ),
  );
}

/** Cheap read-only check for the relay: does the lease file still name us? */
async function leaseStillOurs() {
  const current = await readNodeLease().catch(() => null);
  return Boolean(current && current.node_id === nodeId);
}

async function requireNodeLease() {
  // Re-read on every use: another node taking the lease (e.g. a forced
  // takeover) must stop this one immediately, not at the next heartbeat.
  const current = await readNodeLease();
  if (current && current.node_id === nodeId) nodeLeaseHeld = true;
  else await refreshNodeLease();
  if (!nodeLeaseHeld) {
    throw new HttpError(
      503,
      `another browser-node (${nodeLeaseBlockedBy || "unknown"}) holds this volume's profile lease; refusing (fail closed)`,
    );
  }
}

// Heartbeat / out-of-band lease check. Runs under the lifecycle lock, since
// losing the lease closes profiles (fencing) and must not interleave with an
// open, close, trash or rename.
let leaseRefreshQueued = false;
function scheduleLeaseRefresh() {
  if (leaseRefreshQueued) return;
  leaseRefreshQueued = true;
  withLifecycleLock(() => refreshNodeLease())
    .catch((err) => console.error(`node lease heartbeat failed: ${err.message}`))
    .finally(() => {
      leaseRefreshQueued = false;
    });
}

async function releaseNodeLease() {
  if (!nodeLeaseHeld) return;
  const current = await readNodeLease().catch(() => null);
  if (current && current.node_id === nodeId) await unlink(nodeLeaseFile).catch(() => {});
  nodeLeaseHeld = false;
}

/**
 * Startup recovery: every Chromium lock on the volume was left by a process
 * that no longer exists -- but only if no other node is alive on it. Runs
 * once, after this node has the lease.
 */
async function sweepAllStaleLocks() {
  let entries = [];
  try {
    entries = await readdir(profilesRoot, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name.startsWith(".")) continue;
    const dir = profileDir(entry.name);
    try {
      await assertNotHeldElsewhere(dir);
      const removed = await clearStaleLocks(dir);
      if (removed.length) console.log(`startup: profile '${entry.name}': cleared stale ${removed.join(", ")}`);
    } catch (err) {
      console.error(`startup: left locks of profile '${entry.name}' alone: ${err.message}`);
    }
  }
  await rm(healthcheckRoot, { recursive: true, force: true }).catch(() => {});
}

async function clearStaleLocks(dir) {
  const removed = [];
  for (const file of STALE_LOCK_FILES) {
    const path = join(dir, file);
    try {
      await lstat(path); // lstat: SingletonLock is a dangling symlink
    } catch {
      continue;
    }
    try {
      await unlink(path);
      removed.push(file);
    } catch (err) {
      console.warn(`could not remove stale ${path}: ${err.message}`);
    }
  }
  return removed;
}

/** Refuse to touch a directory some process we do not manage still holds. */
async function assertNotHeldElsewhere(dir) {
  // A profile we just closed can keep a child process alive for a moment.
  let holders = await profileHolderPids(dir);
  for (let attempt = 0; holders.length && attempt < 10; attempt += 1) {
    await sleep(200);
    holders = await profileHolderPids(dir);
  }
  if (holders.length) {
    throw new HttpError(409, `profile directory is held by running process(es) ${holders.join(",")}`);
  }
}

function timestampSlug() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

async function moveToTrash(name, reason) {
  const dir = profileDir(name);
  if (!existsSync(dir)) return null;
  await mkdir(trashRoot, { recursive: true, mode: 0o700 });
  const safeReason = String(reason || "trashed").replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 40) || "trashed";
  let destination = join(trashRoot, `${name}--${timestampSlug()}--${safeReason}`);
  if (existsSync(destination)) destination += `-${randomBytes(3).toString("hex")}`;
  await rename(dir, destination);
  console.log(`persistent profile '${name}' moved to trash: ${destination} (${safeReason})`);
  return destination;
}

/**
 * Chromium writes its actual (OS-assigned, since we ask for port 0) remote
 * debugging port into this file inside the user-data-dir shortly after start.
 * Any stale copy from an earlier run is deleted before launch (see
 * clearStaleLocks), so whatever is read here belongs to this process.
 */
async function discoverCdpPort(userDataDir) {
  const portFile = join(userDataDir, "DevToolsActivePort");
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (existsSync(portFile)) {
      const content = (await readFile(portFile, "utf-8")).trim();
      const [portLine, wsPathLine] = content.split("\n");
      const cdpPort = Number.parseInt(portLine, 10);
      if (Number.isFinite(cdpPort) && wsPathLine) {
        return { localPort: cdpPort, wsPath: wsPathLine.trim() };
      }
    }
    await sleep(250);
  }
  throw new Error(`timed out waiting for DevToolsActivePort under ${userDataDir}`);
}

function relayEndpoint(relayId, wsPath) {
  return `ws://${cdpRelayAdvertisedHost}:${cdpRelayPort}/cdp/${encodeURIComponent(relayId)}${wsPath}`;
}

function persistentLaunchArgs() {
  return [
    `--window-size=${width},${height}`,
    "--disable-dev-shm-usage",
    // No GPU under Xvfb. --disable-software-rasterizer is deliberately NOT
    // passed here: together with --disable-gpu it removes WebGL entirely,
    // and "no WebGL at all" is a far rarer fingerprint than SwiftShader's
    // software WebGL (Playwright already passes --enable-unsafe-swiftshader).
    "--disable-gpu",
    "--disable-background-networking",
    // Keeps navigator.webdriver false natively -- no JS override (an own
    // property on navigator is itself detectable).
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-notifications",
    // Loopback only (Chromium ignores any other address for a headed
    // browser); reached from the controller through the authenticated relay.
    "--remote-debugging-port=0",
  ];
}

async function launchProfile(name, opts, owner) {
  await requireNodeLease();
  const userDataDir = profileDir(name);
  if (existsSync(userDataDir)) {
    await assertNotHeldElsewhere(userDataDir);
    const removed = await clearStaleLocks(userDataDir);
    if (removed.length) console.log(`persistent profile '${name}': cleared stale ${removed.join(", ")}`);
    const marker = await readOwnerMarker(userDataDir);
    if (marker.state === "bad") {
      // Cannot tell whose logins these are: never open them for anyone.
      console.error(`persistent profile '${name}': owner marker unreadable (${marker.error}); moving to trash`);
      await moveToTrash(name, "bad-marker");
    } else if (marker.state === "missing" && (await hasProfileData(userDataDir))) {
      // Data with no owner record (created before markers existed). Only the
      // controller-verified "remember me" default may adopt it; any other
      // name starts fresh and the old data is kept in trash.
      if (!opts.adopt_unmarked) {
        console.error(`persistent profile '${name}': unmarked existing data; moving to trash`);
        await moveToTrash(name, "unmarked");
      }
    } else if (marker.state === "ok" && marker.owner !== null && marker.owner !== owner) {
      // Same name, different owner: the old identity's logins must never
      // open for the new one. Kept (in trash), never reused.
      await moveToTrash(name, "owner-changed");
    }
  }
  // 0700: this directory holds a real, logged-in browser profile.
  await mkdir(userDataDir, { recursive: true, mode: 0o700 });
  const marker = await readOwnerMarker(userDataDir);
  if (marker.state === "missing" || (marker.state === "ok" && marker.owner === null && owner !== null)) {
    await writeOwnerMarker(userDataDir, owner);
  }
  // Checked before launch: Chromium creates "Default" the moment it starts.
  const wasEmpty = !existsSync(join(userDataDir, "Default"));

  const launchOptions = {
    headless: false,
    chromiumSandbox: false,
    // Playwright 1.62 no longer passes --enable-automation, but pin it off
    // explicitly so an upgrade cannot quietly bring the infobar/flag back.
    ignoreDefaultArgs: ["--enable-automation"],
    viewport: opts.viewport || { width, height },
    acceptDownloads: opts.accept_downloads !== false,
    downloadsPath: downloadsDir,
    locale: opts.locale || defaultLocale,
    timezoneId: opts.timezone_id || defaultTimezoneId,
    args: persistentLaunchArgs(),
  };
  if (opts.user_agent) launchOptions.userAgent = opts.user_agent;
  if (opts.extra_http_headers) launchOptions.extraHTTPHeaders = opts.extra_http_headers;
  if (opts.proxy && opts.proxy.server) launchOptions.proxy = opts.proxy;
  // Seed cookies + localStorage from the encrypted "remember me" backup, but
  // ONLY into a profile that has never been launched before. An existing
  // profile holds the real, current, on-disk login state.
  //
  // launchPersistentContext() accepts a `storageState` option and silently
  // ignores it (Playwright 1.62 applies storageState only in newContext();
  // the persistent launch path never calls setStorageState). Passing it there
  // is what made every "seeded" profile start with zero cookies -- the
  // remembered Google login never reached the first persistent profile. So
  // the state is applied explicitly after launch.
  const seeded = Boolean(wasEmpty && opts.storage_state);

  const context = await chromium.launchPersistentContext(userDataDir, launchOptions);
  let discovered;
  try {
    if (seeded) {
      await context.setStorageState(opts.storage_state);
      const cookies = await context.cookies();
      console.log(`persistent profile '${name}': seeded ${cookies.length} cookie(s) from the saved login`);
    }
    discovered = await discoverCdpPort(userDataDir);
  } catch (err) {
    await context.close().catch(() => {});
    if (seeded) {
      // Chromium ran against this directory only to be seeded; leaving it
      // behind would make the next Open treat it as "not empty" and never
      // seed again, silently dropping the saved login for good.
      await rm(userDataDir, { recursive: true, force: true }).catch(() => {});
    }
    throw err;
  }

  const entry = {
    context,
    localPort: discovered.localPort,
    wsPath: discovered.wsPath,
    cdpEndpoint: relayEndpoint(name, discovered.wsPath),
    owner,
    generation: nextGeneration(),
  };
  profiles.set(name, entry);
  relayTargets.set(name, discovered.localPort);
  context.on("close", () => {
    if (profiles.get(name) === entry) {
      profiles.delete(name);
      relayTargets.delete(name);
    }
  });
  console.log(`persistent profile '${name}' opened (${wasEmpty ? "new" : "existing"} profile) on loopback :${discovered.localPort}`);
  return { entry, wasEmpty, seeded };
}

async function openProfile(name, opts) {
  const owner = normalizeOwner(opts.owner);
  await requireNodeLease();
  const existing = profiles.get(name);
  if (existing) {
    if (existing.owner !== null && existing.owner !== owner) {
      throw new HttpError(409, "profile is open for a different owner");
    }
    if (existing.owner === null && owner !== null) {
      existing.owner = owner;
      await writeOwnerMarker(profileDir(name), owner).catch(() => {});
    }
    existing.generation = nextGeneration();
    return { entry: existing, alreadyOpen: true, seeded: false, wasEmpty: false };
  }
  const launched = await launchProfile(name, opts, owner);
  return { entry: launched.entry, alreadyOpen: false, seeded: launched.seeded, wasEmpty: launched.wasEmpty };
}

async function closeProfile(name, generation = undefined) {
  const entry = profiles.get(name);
  if (!entry) return { closed: true, existed: false };
  if (generation !== undefined && generation !== entry.generation) {
    console.log(`persistent profile '${name}': ignored close for generation ${generation} (current ${entry.generation})`);
    return { closed: false, existed: true, stale_generation: true };
  }
  profiles.delete(name);
  relayTargets.delete(name);
  try {
    await entry.context.close();
  } catch (err) {
    console.error(`error closing persistent profile '${name}':`, err);
  }
  return { closed: true, existed: true };
}

async function assertOwnerAllows(dir, owner, { allowBadMarker = false } = {}) {
  const marker = await readOwnerMarker(dir);
  if (marker.state === "bad") {
    if (allowBadMarker) return;
    throw new HttpError(409, `profile directory has an unreadable owner marker (${marker.error})`);
  }
  if (marker.owner !== null && marker.owner !== owner) {
    throw new HttpError(403, "profile directory belongs to a different owner");
  }
}

async function trashProfile(name, reason, owner) {
  const dir = profileDir(name);
  if (!existsSync(dir)) return { trashed: false, existed: false, was_open: false };
  await requireNodeLease();
  // Trash is the fail-safe direction (nothing is lost), so an unreadable
  // marker does not block it -- it only must not open or rename the data.
  await assertOwnerAllows(dir, owner, { allowBadMarker: true });
  const wasOpen = profiles.has(name);
  if (wasOpen) await closeProfile(name);
  await assertNotHeldElsewhere(dir);
  const destination = await moveToTrash(name, reason);
  return { trashed: true, existed: true, was_open: wasOpen, trash_path: destination };
}

async function renameProfile(name, newName, owner) {
  const source = profileDir(name);
  const destination = profileDir(newName);
  if (!existsSync(source)) {
    // Nothing on disk for the old name. A stale directory already sitting
    // under the new name must still not survive into the renamed identity.
    const replaced = existsSync(destination) ? await trashProfile(newName, "replaced-by-rename", owner) : null;
    return { renamed: false, existed: false, replaced_destination: Boolean(replaced && replaced.trashed) };
  }
  await requireNodeLease();
  await assertOwnerAllows(source, owner);
  if (existsSync(destination)) await assertOwnerAllows(destination, owner, { allowBadMarker: true });
  if (profiles.has(name)) await closeProfile(name);
  if (profiles.has(newName)) await closeProfile(newName);
  await assertNotHeldElsewhere(source);
  let replacedDestination = false;
  if (existsSync(destination)) {
    await assertNotHeldElsewhere(destination);
    await moveToTrash(newName, "replaced-by-rename");
    replacedDestination = true;
  }
  await rename(source, destination);
  console.log(`persistent profile '${name}' renamed to '${newName}'`);
  return { renamed: true, existed: true, replaced_destination: replacedDestination };
}

// ---------------------------------------------------------------------------
// Deep health: proves launch -> DevToolsActivePort -> relay -> CDP attach
// works end to end, on a disposable profile (never a real one). Headless on
// purpose (same full Chromium binary via channel "chromium") so the check
// never flashes a window on the owner's live noVNC view. Cached, and only
// one run in flight, so the container healthcheck stays cheap.
let deepHealth = { at: 0, ok: false, error: "not run yet" };
// Set once the shared launchServer() browser below is up.
let legacyEndpoint = null;
let deepHealthInFlight = null;

async function runDeepHealthcheck() {
  const id = `~hc-${randomBytes(6).toString("hex")}`;
  const dir = join(healthcheckRoot, id.slice(1));
  let context = null;
  let browser = null;
  const started = Date.now();
  try {
    await requireNodeLease();
    await mkdir(dir, { recursive: true, mode: 0o700 });
    if (process.platform === "linux") {
      // Exercise the crash-recovery path every time: a lock left by a
      // container that no longer exists must not block the launch.
      const { symlink } = await import("node:fs/promises");
      await symlink("stale-host-that-no-longer-exists-99999", join(dir, "SingletonLock")).catch(() => {});
    }
    await clearStaleLocks(dir);
    context = await chromium.launchPersistentContext(dir, {
      headless: true,
      channel: "chromium",
      chromiumSandbox: false,
      ignoreDefaultArgs: ["--enable-automation"],
      args: persistentLaunchArgs(),
    });
    const discovered = await discoverCdpPort(dir);
    relayTargets.set(id, discovered.localPort);
    browser = await chromium.connectOverCDP(`ws://127.0.0.1:${cdpRelayPort}/cdp/${encodeURIComponent(id)}${discovered.wsPath}`, {
      headers: { Authorization: `Bearer ${profileControlToken}` },
      timeout: 15000,
    });
    if (!browser.contexts().length) throw new Error("relay attach exposed no browser context");
    if (!legacyEndpoint || !existsSync(endpointFile)) throw new Error("legacy browser server is not up");
    return { ok: true, elapsed_ms: Date.now() - started, mode: "disposable-launch" };
  } finally {
    relayTargets.delete(id);
    if (browser) await browser.close().catch(() => {});
    if (context) await context.close().catch(() => {});
    // A throwaway profile with no logins in it -- safe to remove outright.
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

// While a real profile is open the launch path is already proven by it, and a
// second (disposable) Chromium beside the owner's live one only competes with
// it for the container's pids and memory. Probe the open profiles' own CDP
// endpoints through the relay instead -- the exact hop the controller uses.
function relayJsonVersion(relayId) {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: "127.0.0.1",
        port: cdpRelayPort,
        path: `/cdp/${encodeURIComponent(relayId)}/json/version`,
        headers: { Authorization: `Bearer ${profileControlToken}` },
        timeout: 5000,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          if (res.statusCode !== 200) return reject(new Error(`relay answered ${res.statusCode} for '${relayId}'`));
          try {
            const parsed = JSON.parse(data);
            if (!parsed.webSocketDebuggerUrl) throw new Error("no webSocketDebuggerUrl");
            resolve(parsed);
          } catch (err) {
            reject(new Error(`relay returned an invalid /json/version for '${relayId}': ${err.message}`));
          }
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error(`relay /json/version timed out for '${relayId}'`)));
    req.end();
  });
}

async function runLiveProfilesHealthcheck(names) {
  const started = Date.now();
  await requireNodeLease();
  for (const name of names) await relayJsonVersion(name);
  if (!legacyEndpoint || !existsSync(endpointFile)) throw new Error("legacy browser server is not up");
  return { ok: true, elapsed_ms: Date.now() - started, mode: "live-profiles", profiles: names.length };
}

async function deepHealthcheck() {
  const ttl = deepHealth.ok ? deepHealthTtlMs : deepHealthFailureTtlMs;
  if (deepHealth.at && Date.now() - deepHealth.at < ttl) {
    return { ...deepHealth, cached: true };
  }
  if (!deepHealthInFlight) {
    const openNames = [...profiles.keys()];
    const run = openNames.length ? runLiveProfilesHealthcheck(openNames) : runDeepHealthcheck();
    deepHealthInFlight = run
      .then((result) => {
        deepHealth = {
          at: Date.now(),
          ok: true,
          error: null,
          elapsed_ms: result.elapsed_ms,
          mode: result.mode || "disposable-launch",
        };
      })
      .catch((err) => {
        deepHealth = { at: Date.now(), ok: false, error: (err && err.message) || String(err) };
      })
      .finally(() => {
        deepHealthInFlight = null;
      });
  }
  await deepHealthInFlight;
  return { ...deepHealth, cached: false };
}

// ---------------------------------------------------------------------------
// HTTP plumbing

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 5_000_000) {
        reject(new HttpError(413, "request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch {
        reject(new HttpError(400, "invalid JSON body"));
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

function requireName(value, field = "name") {
  const name = String(value || "");
  if (!PROFILE_NAME_RE.test(name)) throw new HttpError(400, `invalid profile ${field}`);
  return name;
}

// Control API, reachable only on the internal tenant network (never
// published). /healthz is unauthenticated and reveals nothing; everything
// else requires the bearer token.
const controlServer = createServer(async (req, res) => {
  try {
    const path = (req.url || "").split("?")[0];
    if (req.method === "GET" && path === "/healthz") {
      return sendJson(res, 200, {
        ok: true,
        persistent_profiles_enabled: persistentProfilesEnabled,
        control_token_configured: Boolean(profileControlToken),
      });
    }
    if (!persistentProfilesEnabled) {
      return sendJson(res, 404, { error: "persistent profiles are disabled (PERSISTENT_PROFILES_ENABLED=false)" });
    }
    if (!profileControlToken) {
      return sendJson(res, 503, { error: "PROFILE_CONTROL_TOKEN is not configured; refusing profile control" });
    }
    if (!tokenMatches(req.headers.authorization)) {
      return sendJson(res, 401, { error: "unauthorized" });
    }
    if (req.method === "GET" && path === "/healthz/deep") {
      const result = await deepHealthcheck();
      return sendJson(res, result.ok ? 200 : 503, result);
    }
    if (req.method !== "POST") return sendJson(res, 404, { error: "not found" });
    const body = await readJsonBody(req);
    if (path === "/profiles/open") {
      const name = requireName(body.name);
      const result = await withLifecycleLock(() => openProfile(name, body));
      return sendJson(res, 200, {
        cdp_endpoint: result.entry.cdpEndpoint,
        generation: result.entry.generation,
        already_open: result.alreadyOpen,
        seeded: result.seeded,
        was_empty: result.wasEmpty,
      });
    }
    if (path === "/profiles/close") {
      const name = requireName(body.name);
      if (typeof body.generation !== "string" || !body.generation) {
        throw new HttpError(400, "generation is required");
      }
      return sendJson(res, 200, await withLifecycleLock(() => closeProfile(name, body.generation)));
    }
    if (path === "/profiles/trash") {
      const name = requireName(body.name);
      const owner = normalizeOwner(body.owner);
      return sendJson(res, 200, await withLifecycleLock(() => trashProfile(name, body.reason, owner)));
    }
    if (path === "/profiles/rename") {
      const name = requireName(body.name);
      const newName = requireName(body.new_name, "new_name");
      if (name === newName) throw new HttpError(400, "new_name must differ from name");
      const owner = normalizeOwner(body.owner);
      return sendJson(res, 200, await withLifecycleLock(() => renameProfile(name, newName, owner)));
    }
    return sendJson(res, 404, { error: "not found" });
  } catch (err) {
    const status = err instanceof HttpError ? err.status : 500;
    if (status >= 500) console.error("profile control server error:", err);
    return sendJson(res, status, { error: (err && err.message) || String(err) });
  }
});

// ---------------------------------------------------------------------------
// CDP relay: ws://browser-node:9225/cdp/<name>/devtools/browser/<id>
//   -> ws://127.0.0.1:<profile's loopback port>/devtools/browser/<id>

const RELAY_PATH_RE = /^\/cdp\/([^/]+)(\/.*)$/;

function parseRelayPath(url) {
  const match = RELAY_PATH_RE.exec((url || "").split("?")[0]);
  if (!match) return null;
  let id;
  try {
    id = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const localPort = relayTargets.get(id);
  if (!localPort) return null;
  return { id, localPort, upstreamPath: match[2] };
}

function rejectUpgrade(socket, status, reason) {
  socket.end(`HTTP/1.1 ${status} ${reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
}

const relayServer = createServer(async (req, res) => {
  if (!profileControlToken || !tokenMatches(req.headers.authorization)) {
    return sendJson(res, 401, { error: "unauthorized" });
  }
  const target = parseRelayPath(req.url);
  if (!target || req.method !== "GET" || target.upstreamPath !== "/json/version") {
    return sendJson(res, 404, { error: "not found" });
  }
  if (!(await leaseStillOurs())) {
    scheduleLeaseRefresh();
    return sendJson(res, 503, { error: "this browser-node no longer holds the profile lease" });
  }
  const upstream = httpRequest(
    {
      host: "127.0.0.1",
      port: target.localPort,
      path: "/json/version",
      // Chromium rejects DevTools HTTP requests whose Host is not localhost/IP.
      headers: { Host: `127.0.0.1:${target.localPort}` },
      timeout: 5000,
    },
    (upstreamRes) => {
      let data = "";
      upstreamRes.on("data", (chunk) => {
        data += chunk;
      });
      upstreamRes.on("end", () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.webSocketDebuggerUrl) {
            const wsPath = new URL(parsed.webSocketDebuggerUrl).pathname;
            parsed.webSocketDebuggerUrl = relayEndpoint(target.id, wsPath);
          }
          sendJson(res, 200, parsed);
        } catch {
          sendJson(res, 502, { error: "invalid upstream response" });
        }
      });
    },
  );
  upstream.on("error", () => sendJson(res, 502, { error: "upstream unavailable" }));
  upstream.on("timeout", () => upstream.destroy(new Error("timeout")));
  upstream.end();
});

relayServer.on("upgrade", async (req, socket, head) => {
  socket.on("error", () => socket.destroy());
  if (!profileControlToken || !tokenMatches(req.headers.authorization)) {
    return rejectUpgrade(socket, 401, "Unauthorized");
  }
  if (!parseRelayPath(req.url)) return rejectUpgrade(socket, 404, "Not Found");
  if (!(await leaseStillOurs())) {
    scheduleLeaseRefresh();
    return rejectUpgrade(socket, 503, "Service Unavailable");
  }
  // Re-resolved after the await: fencing may have removed the target.
  const target = parseRelayPath(req.url);
  if (!target || !target.upstreamPath.startsWith("/devtools/")) {
    return rejectUpgrade(socket, 404, "Not Found");
  }
  let tracked = relaySockets.get(target.id);
  if (!tracked) relaySockets.set(target.id, (tracked = new Set()));
  tracked.add(socket);
  socket.on("close", () => tracked.delete(socket));
  const upstream = netConnect({ host: "127.0.0.1", port: target.localPort });
  const teardown = () => {
    socket.destroy();
    upstream.destroy();
  };
  upstream.on("error", teardown);
  upstream.on("close", () => socket.destroy());
  socket.on("close", () => upstream.destroy());
  upstream.on("connect", () => {
    socket.setNoDelay(true);
    upstream.setNoDelay(true);
    const lines = [`GET ${target.upstreamPath} HTTP/1.1`];
    const raw = req.rawHeaders;
    for (let i = 0; i < raw.length; i += 2) {
      const key = raw[i].toLowerCase();
      // The bearer token never reaches Chromium; Host/Origin are rewritten
      // so Chromium's DNS-rebinding / origin checks see a loopback client.
      if (key === "host" || key === "authorization" || key === "origin") continue;
      lines.push(`${raw[i]}: ${raw[i + 1]}`);
    }
    lines.push(`Host: 127.0.0.1:${target.localPort}`);
    upstream.write(`${lines.join("\r\n")}\r\n\r\n`);
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
});

async function listen(server, portNumber, hostName) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(portNumber, hostName, resolve);
  });
}

await listen(controlServer, profileControlPort, profileControlHost);
console.log(
  `profile control server listening on ${profileControlHost}:${profileControlPort} ` +
    `(persistent profiles ${persistentProfilesEnabled ? "enabled" : "disabled"})`,
);
if (persistentProfilesEnabled) {
  await mkdir(profilesRoot, { recursive: true, mode: 0o700 });
  if (await refreshNodeLease({ force: nodeLeaseForce })) {
    await sweepAllStaleLocks();
  }
  setInterval(scheduleLeaseRefresh, nodeLeaseHeartbeatMs).unref();
  await listen(relayServer, cdpRelayPort, cdpRelayHost);
  console.log(`CDP relay listening on ${cdpRelayHost}:${cdpRelayPort}`);
  if (!profileControlToken) {
    console.error("PROFILE_CONTROL_TOKEN is empty: profile control and CDP relay will refuse every request");
  }
}

// The shared launchServer() browser. Always started, in both modes: with
// persistent profiles on, it still serves the sessions that must NOT attach
// to a named on-disk profile -- fork() / an explicit storage_state_path (an
// independent clone), and an Open whose caller is not allowed to use the
// remembered login (a plain fresh context, exactly the pre-profile
// behaviour). Idle, it has no window (Playwright passes --no-startup-window).
const legacyBrowserServer = await chromium.launchServer({
  headless: false,
  chromiumSandbox: false,
  host,
  port,
  downloadsPath: downloadsDir,
  args: [
    `--window-size=${width},${height}`,
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--lang=en-US,en",
    "--disable-notifications",
  ],
});

const rawEndpoint = new URL(legacyBrowserServer.wsEndpoint());
rawEndpoint.hostname = advertisedHost;
rawEndpoint.port = String(port);
legacyEndpoint = rawEndpoint.toString();

await mkdir(dirname(endpointFile), { recursive: true });
const tmpFile = `${endpointFile}.tmp`;
await writeFile(tmpFile, legacyEndpoint, "utf-8");
await rename(tmpFile, endpointFile);
console.log(`wrote ${endpointFile}: ${legacyEndpoint}`);

let shuttingDown = false;
async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  await Promise.all([...profiles.values()].map((entry) => entry.context.close().catch(() => {})));
  // Profiles are closed: hand the volume over at once instead of making the
  // next container wait out the stale window.
  await releaseNodeLease().catch(() => {});
  await legacyBrowserServer.close().catch(() => {});
  process.exit(0);
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    shutdown();
  });
}

await new Promise((resolve) => legacyBrowserServer.on("close", resolve));
