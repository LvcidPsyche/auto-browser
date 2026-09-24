import { createServer } from "node:http";
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { chromium } from "playwright";

const width = Number.parseInt(process.env.BROWSER_WIDTH || "1280", 10);
const height = Number.parseInt(process.env.BROWSER_HEIGHT || "800", 10);
const endpointFile = process.env.BROWSER_WS_ENDPOINT_FILE || "/data/profile/browser-ws-endpoint.txt";
const host = process.env.PLAYWRIGHT_SERVER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.PLAYWRIGHT_SERVER_PORT || "9223", 10);
const advertisedHost = process.env.PLAYWRIGHT_SERVER_ADVERTISED_HOST || "browser-node";

// Persistent Chromium profiles -- one named identity's on-disk user-data-dir
// (owner-default, nihad-google, ...), launched on demand and reused for as
// long as it stays open, so IndexedDB/service workers/cache/history survive
// across Opens, controller restarts and image rebuilds. See
// controller/app/persistent_profiles.py for the client side of this
// protocol, and the PERSISTENT_PROFILES_ENABLED rollback flag: unset (the
// code default), this whole feature is inert and the container behaves
// exactly as before -- the shared chromium.launchServer() below boots the
// same way it always has.
const persistentProfilesEnabled = (process.env.PERSISTENT_PROFILES_ENABLED || "false").toLowerCase() === "true";
const profileControlHost = process.env.PROFILE_CONTROL_HOST || "0.0.0.0";
const profileControlPort = Number.parseInt(process.env.PROFILE_CONTROL_PORT || "9224", 10);
const profilesRoot = process.env.BROWSER_PROFILES_ROOT || "/data/browser-profiles";
// A real device's language/timezone do not change between logins; the
// container's own default (en-US/UTC) is a bigger tell than an unset user
// agent, so a real, headed Chromium's own UA is left alone unless
// PERSISTENT_PROFILE_USER_AGENT is set (env-configurable per the incident
// report; ar-EG / Africa/Cairo are the sensible defaults for this owner).
const defaultLocale = process.env.PERSISTENT_PROFILE_LOCALE || "ar-EG";
const defaultTimezoneId = process.env.PERSISTENT_PROFILE_TIMEZONE || "Africa/Cairo";

const PROFILE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// name -> { context, cdpEndpoint, refCount }
const profiles = new Map();
// name -> in-flight launch Promise, so two near-simultaneous opens of the
// same never-yet-open profile await the one real launch instead of racing
// two Chromium processes onto the same user-data-dir (which Chromium's own
// profile lock would refuse anyway, but noisily).
const launching = new Map();

/**
 * Chromium writes its actual (OS-assigned, since we ask for port 0) remote
 * debugging port into this file inside the user-data-dir shortly after
 * start. This is the standard way to discover a CDP endpoint for a browser
 * launched with an explicit user-data-dir -- there is no equivalent of
 * launchServer()'s wsEndpoint() for a persistent context.
 */
async function discoverCdpEndpoint(userDataDir) {
  const portFile = join(userDataDir, "DevToolsActivePort");
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (existsSync(portFile)) {
      const content = (await readFile(portFile, "utf-8")).trim();
      const [portLine, wsPathLine] = content.split("\n");
      const cdpPort = Number.parseInt(portLine, 10);
      if (Number.isFinite(cdpPort) && wsPathLine) {
        return `ws://${advertisedHost}:${cdpPort}${wsPathLine}`;
      }
    }
    await sleep(250);
  }
  throw new Error(`timed out waiting for DevToolsActivePort under ${userDataDir}`);
}

async function launchProfile(name, opts) {
  const userDataDir = join(profilesRoot, name);
  // 0700: this directory holds a real, logged-in browser profile (cookies,
  // IndexedDB, history) for one named identity -- readable/writable only by
  // the browser user that owns it (this process itself, uid 10001; see
  // entrypoint.sh), not by anything else that might land in this container.
  await mkdir(userDataDir, { recursive: true, mode: 0o700 });
  // Checked before launch: Chromium creates its own "Default" directory the
  // moment it starts, so this is the last point at which "does this profile
  // already have real content" can still be answered.
  const wasEmpty = !existsSync(join(userDataDir, "Default"));

  const launchOptions = {
    headless: false,
    chromiumSandbox: false,
    viewport: opts.viewport || { width, height },
    acceptDownloads: opts.accept_downloads !== false,
    downloadsPath: "/data/downloads",
    locale: opts.locale || defaultLocale,
    timezoneId: opts.timezone_id || defaultTimezoneId,
    args: [
      `--window-size=${width},${height}`,
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--disable-software-rasterizer",
      "--disable-background-networking",
      "--disable-blink-features=AutomationControlled",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-notifications",
      // Bound to 0.0.0.0 (not loopback) so the controller container can
      // reach it over the tenant-private Docker network -- the same
      // exposure the shared launchServer() below already has on 9223.
      "--remote-debugging-address=0.0.0.0",
      "--remote-debugging-port=0",
    ],
  };
  if (opts.user_agent) launchOptions.userAgent = opts.user_agent;
  if (opts.extra_http_headers) launchOptions.extraHTTPHeaders = opts.extra_http_headers;
  if (opts.proxy && opts.proxy.server) launchOptions.proxy = opts.proxy;
  // Seed cookies + localStorage from the encrypted "remember me" backup, but
  // ONLY into a profile that has never been launched before. A profile that
  // already exists holds the real, current, on-disk login state -- replaying
  // a possibly-stale export over it is exactly the silent-downgrade the
  // auto-persist guard on the controller side already refuses to do.
  const seeded = Boolean(wasEmpty && opts.storage_state);
  if (seeded) launchOptions.storageState = opts.storage_state;

  const context = await chromium.launchPersistentContext(userDataDir, launchOptions);
  let cdpEndpoint;
  try {
    cdpEndpoint = await discoverCdpEndpoint(userDataDir);
  } catch (err) {
    await context.close().catch(() => {});
    throw err;
  }

  const entry = { context, cdpEndpoint, refCount: 1 };
  profiles.set(name, entry);
  context.on("close", () => {
    profiles.delete(name);
  });
  console.log(`persistent profile '${name}' opened (${wasEmpty ? "new" : "existing"} profile): ${cdpEndpoint}`);
  return { cdpEndpoint, wasEmpty, seeded };
}

async function acquireProfile(name, opts) {
  const existing = profiles.get(name);
  if (existing) {
    existing.refCount += 1;
    return { cdpEndpoint: existing.cdpEndpoint, alreadyOpen: true, seeded: false, wasEmpty: false };
  }

  const joinedExisting = launching.has(name);
  const promise = joinedExisting ? launching.get(name) : launchProfile(name, opts);
  if (!joinedExisting) launching.set(name, promise);
  try {
    const launched = await promise;
    if (joinedExisting) {
      // The launch we joined already registered itself in `profiles`.
      const entry = profiles.get(name);
      entry.refCount += 1;
      return { cdpEndpoint: entry.cdpEndpoint, alreadyOpen: true, seeded: false, wasEmpty: false };
    }
    return { cdpEndpoint: launched.cdpEndpoint, alreadyOpen: false, seeded: launched.seeded, wasEmpty: launched.wasEmpty };
  } finally {
    if (launching.get(name) === promise) launching.delete(name);
  }
}

async function releaseProfile(name) {
  const entry = profiles.get(name);
  if (!entry) return { closed: true, existed: false };
  entry.refCount -= 1;
  if (entry.refCount > 0) return { closed: false, existed: true, ref_count: entry.refCount };
  profiles.delete(name);
  try {
    await entry.context.close();
  } catch (err) {
    console.error(`error closing persistent profile '${name}':`, err);
  }
  return { closed: true, existed: true };
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 5_000_000) {
        reject(new Error("request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch (err) {
        reject(err);
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

// A small always-on control API, reachable only on the internal tenant
// network (never published to the host -- see deploy/tenants/compose.yml).
// /healthz is served even when persistent profiles are disabled, so the
// container healthcheck can rely on this port in both modes; /profiles/*
// only does anything when PERSISTENT_PROFILES_ENABLED=true.
const controlServer = createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/healthz") {
      return sendJson(res, 200, { ok: true, persistent_profiles_enabled: persistentProfilesEnabled });
    }
    if (!persistentProfilesEnabled) {
      return sendJson(res, 404, { error: "persistent profiles are disabled (PERSISTENT_PROFILES_ENABLED=false)" });
    }
    if (req.method === "POST" && req.url === "/profiles/open") {
      const body = await readJsonBody(req);
      const name = String(body.name || "");
      if (!PROFILE_NAME_RE.test(name)) {
        return sendJson(res, 400, { error: "invalid profile name" });
      }
      const result = await acquireProfile(name, body);
      return sendJson(res, 200, {
        cdp_endpoint: result.cdpEndpoint,
        already_open: result.alreadyOpen,
        seeded: result.seeded,
        was_empty: result.wasEmpty,
      });
    }
    if (req.method === "POST" && req.url === "/profiles/close") {
      const body = await readJsonBody(req);
      const name = String(body.name || "");
      if (!PROFILE_NAME_RE.test(name)) {
        return sendJson(res, 400, { error: "invalid profile name" });
      }
      const result = await releaseProfile(name);
      return sendJson(res, 200, result);
    }
    return sendJson(res, 404, { error: "not found" });
  } catch (err) {
    console.error("profile control server error:", err);
    return sendJson(res, 500, { error: (err && err.message) || String(err) });
  }
});

await new Promise((resolve, reject) => {
  controlServer.once("error", reject);
  controlServer.listen(profileControlPort, profileControlHost, resolve);
});
console.log(
  `profile control server listening on ${profileControlHost}:${profileControlPort} ` +
    `(persistent profiles ${persistentProfilesEnabled ? "enabled" : "disabled"})`,
);

let legacyBrowserServer = null;

if (!persistentProfilesEnabled) {
  // Exactly today's behaviour: one shared Chromium process, one context per
  // session (see controller/app/browser/services/sessions.py), driven over
  // Playwright's own server protocol.
  legacyBrowserServer = await chromium.launchServer({
    headless: false,
    chromiumSandbox: false,
    host,
    port,
    downloadsPath: "/data/downloads",
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
  const advertisedEndpoint = rawEndpoint.toString();

  await mkdir(dirname(endpointFile), { recursive: true });
  const tmpFile = `${endpointFile}.tmp`;
  await writeFile(tmpFile, advertisedEndpoint, "utf-8");
  await rename(tmpFile, endpointFile);
  console.log(`wrote ${endpointFile}: ${advertisedEndpoint}`);
}

async function shutdown() {
  if (legacyBrowserServer) {
    await legacyBrowserServer.close().catch(() => {});
  }
  await Promise.all([...profiles.values()].map((entry) => entry.context.close().catch(() => {})));
  process.exit(0);
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    shutdown();
  });
}

if (legacyBrowserServer) {
  await new Promise((resolve) => legacyBrowserServer.on("close", resolve));
} else {
  // Nothing else to await -- the open control-server socket keeps the
  // process alive; persistent profiles are launched lazily on demand.
  await new Promise(() => {});
}
