// Real-Chromium tests for server.mjs: spawns the actual server with a real
// Playwright Chromium, then drives it from THIS (separate) process over the
// machine's LAN address -- the same shape as the controller container
// reaching browser-node over the tenant network.
//
//   cd browser-node && npm ci && npx playwright install chromium
//   node --test test/
//
// Needs a display for the headed profile browsers (any desktop, or Xvfb).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { connect as netConnect } from "node:net";
import { networkInterfaces, tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const TOKEN = "test-token-" + Math.random().toString(16).slice(2);
const CONTROL_PORT = 19224;
const RELAY_PORT = 19225;
const LEGACY_PORT = 19223;

function lanAddress() {
  for (const addrs of Object.values(networkInterfaces())) {
    for (const addr of addrs || []) {
      if (addr.family === "IPv4" && !addr.internal) return addr.address;
    }
  }
  return null;
}

const LAN = lanAddress();
const root = mkdtempSync(join(tmpdir(), "bn-real-"));
const profilesRoot = join(root, "browser-profiles");
let server;
let serverLog = "";

async function control(path, body, { token = TOKEN, method = "POST" } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`http://${LAN}:${CONTROL_PORT}${path}`, {
    method,
    headers,
    body: method === "POST" ? JSON.stringify(body || {}) : undefined,
  });
  const result = { status: response.status, body: await response.json() };
  if (path === "/profiles/open" && result.status === 200) lastGeneration.set(body.name, result.body.generation);
  return result;
}

// Latest lease generation handed out per profile name.
const lastGeneration = new Map();
function closeLatest(name) {
  return control("/profiles/close", { name, generation: lastGeneration.get(name) });
}

async function attach(endpoint, token = TOKEN) {
  return chromium.connectOverCDP(endpoint, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    timeout: 15000,
  });
}

function tcpReachable(host, port) {
  return new Promise((resolve) => {
    const socket = netConnect({ host, port });
    socket.setTimeout(2000);
    socket.on("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.on("error", () => resolve(false));
    socket.on("timeout", () => {
      socket.destroy();
      resolve(false);
    });
  });
}

before(async () => {
  assert.ok(LAN, "needs a non-loopback IPv4 address to prove cross-host reachability");
  const serverPath = fileURLToPath(new URL("../server.mjs", import.meta.url));
  server = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      PERSISTENT_PROFILES_ENABLED: "true",
      PROFILE_CONTROL_TOKEN: TOKEN,
      PROFILE_CONTROL_HOST: "0.0.0.0",
      PROFILE_CONTROL_PORT: String(CONTROL_PORT),
      PROFILE_CDP_RELAY_HOST: "0.0.0.0",
      PROFILE_CDP_RELAY_PORT: String(RELAY_PORT),
      PROFILE_CDP_RELAY_ADVERTISED_HOST: LAN,
      PLAYWRIGHT_SERVER_HOST: "127.0.0.1",
      PLAYWRIGHT_SERVER_PORT: String(LEGACY_PORT),
      PLAYWRIGHT_SERVER_ADVERTISED_HOST: "127.0.0.1",
      BROWSER_WS_ENDPOINT_FILE: join(root, "profile", "ws.txt"),
      BROWSER_PROFILES_ROOT: profilesRoot,
      BROWSER_DOWNLOADS_DIR: join(root, "downloads"),
      PROFILE_DEEP_HEALTH_TTL_SECONDS: "0",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (chunk) => {
    serverLog += chunk;
  });
  server.stderr.on("data", (chunk) => {
    serverLog += chunk;
  });
  for (let i = 0; i < 120; i += 1) {
    if (existsSync(join(root, "profile", "ws.txt"))) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`server did not start:\n${serverLog}`);
});

after(async () => {
  server?.kill("SIGTERM");
  await new Promise((resolve) => setTimeout(resolve, 1500));
  try {
    rmSync(root, { recursive: true, force: true });
  } catch {
    // Windows may still hold a file for a moment; the OS temp dir is fine.
  }
});

test("control API refuses missing or wrong bearer token", async () => {
  assert.equal((await control("/profiles/open", { name: "alpha" }, { token: null })).status, 401);
  assert.equal((await control("/profiles/open", { name: "alpha" }, { token: "wrong" })).status, 401);
  const health = await control("/healthz", null, { token: null, method: "GET" });
  assert.equal(health.status, 200);
  assert.equal(health.body.control_token_configured, true);
});

test("relay: a separate process attaches over the LAN address; raw CDP port is loopback-only", async () => {
  const opened = await control("/profiles/open", { name: "alpha", owner: "op1" });
  assert.equal(opened.status, 200, JSON.stringify(opened.body));
  const endpoint = opened.body.cdp_endpoint;
  assert.match(endpoint, new RegExp(`^ws://${LAN.replace(/\./g, "\\.")}:${RELAY_PORT}/cdp/alpha/devtools/browser/`));
  assert.equal(opened.body.already_open, false);

  const portLine = readFileSync(join(profilesRoot, "alpha", "DevToolsActivePort"), "utf-8").split("\n")[0];
  const loopbackPort = Number(portLine);
  assert.equal(await tcpReachable("127.0.0.1", loopbackPort), true);
  assert.equal(await tcpReachable(LAN, loopbackPort), false, "Chromium's CDP port must not be reachable off-host");

  await assert.rejects(attach(endpoint, null), "relay must refuse an unauthenticated websocket");
  await assert.rejects(attach(endpoint, "wrong"));

  const version = await fetch(`http://${LAN}:${RELAY_PORT}/cdp/alpha/json/version`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  }).then((r) => r.json());
  assert.match(version.webSocketDebuggerUrl, new RegExp(`^ws://${LAN.replace(/\./g, "\\.")}:${RELAY_PORT}/cdp/alpha/`));

  const browser = await attach(endpoint);
  try {
    assert.equal(browser.contexts().length, 1);
    const context = browser.contexts()[0];
    const page = context.pages()[0] || (await context.newPage());
    await page.goto("data:text/html,<title>t</title>");
    const probe = await page.evaluate(() => {
      const canvas = document.createElement("canvas");
      return {
        webdriver: navigator.webdriver,
        ownWebdriver: Object.getOwnPropertyDescriptor(navigator, "webdriver") !== undefined,
        webgl: Boolean(canvas.getContext("webgl")),
      };
    });
    assert.equal(probe.webdriver, false);
    assert.equal(probe.ownWebdriver, false, "no JS override: webdriver must not be an own property");
    console.log("fingerprint probe:", JSON.stringify(probe));
  } finally {
    await browser.close();
  }
});

test("open twice reuses the running profile; close once ends it; state survives", async () => {
  const first = await control("/profiles/open", { name: "alpha", owner: "op1" });
  assert.equal(first.body.already_open, true);
  const browser = await attach(first.body.cdp_endpoint);
  const context = browser.contexts()[0];
  const page = context.pages()[0] || (await context.newPage());
  await page.goto(`http://${LAN}:${CONTROL_PORT}/healthz`);
  await page.evaluate(() => localStorage.setItem("persisted", "yes"));
  await browser.close();

  const again = await control("/profiles/open", { name: "alpha", owner: "op1" });
  assert.equal(again.body.already_open, true);
  assert.equal(again.body.cdp_endpoint, first.body.cdp_endpoint);
  assert.ok(again.body.generation > first.body.generation);

  assert.equal((await control("/profiles/close", { name: "alpha" })).status, 400, "close needs a generation");
  const closed = await closeLatest("alpha");
  assert.deepEqual(closed.body, { closed: true, existed: true });
  const closedAgain = await closeLatest("alpha");
  assert.deepEqual(closedAgain.body, { closed: true, existed: false });

  const reopened = await control("/profiles/open", { name: "alpha", owner: "op1" });
  assert.equal(reopened.body.already_open, false);
  assert.equal(reopened.body.was_empty, false);
  const b2 = await attach(reopened.body.cdp_endpoint);
  const p2 = b2.contexts()[0].pages()[0] || (await b2.contexts()[0].newPage());
  await p2.goto(`http://${LAN}:${CONTROL_PORT}/healthz`);
  assert.equal(await p2.evaluate(() => localStorage.getItem("persisted")), "yes");
  await b2.close();
  await closeLatest("alpha");
});

test("stale SingletonLock/DevToolsActivePort from a dead container do not block launch", async () => {
  const dir = join(profilesRoot, "alpha");
  symlinkSync("old-container-hostname-99999", join(dir, "SingletonLock"));
  symlinkSync("/tmp/old-container/SingletonSocket", join(dir, "SingletonSocket"));
  writeFileSync(join(dir, "DevToolsActivePort"), "1\n/devtools/browser/stale-id\n");
  const opened = await control("/profiles/open", { name: "alpha", owner: "op1" });
  assert.equal(opened.status, 200, JSON.stringify(opened.body));
  assert.doesNotMatch(opened.body.cdp_endpoint, /stale-id/);
  const browser = await attach(opened.body.cdp_endpoint);
  assert.equal(browser.contexts().length, 1);
  await browser.close();
  await closeLatest("alpha");
  assert.match(serverLog, /cleared stale SingletonLock, SingletonSocket, DevToolsActivePort/);
});

test("same name under a different owner never reuses the old directory", async () => {
  const opened = await control("/profiles/open", { name: "alpha", owner: "op2" });
  assert.equal(opened.status, 200);
  assert.equal(opened.body.was_empty, true);
  const trashed = readdirSync(join(profilesRoot, ".trash"));
  assert.ok(trashed.some((n) => n.startsWith("alpha--") && n.endsWith("--owner-changed")), trashed.join(","));
  const browser = await attach(opened.body.cdp_endpoint);
  const page = browser.contexts()[0].pages()[0] || (await browser.contexts()[0].newPage());
  await page.goto(`http://${LAN}:${CONTROL_PORT}/healthz`);
  assert.equal(await page.evaluate(() => localStorage.getItem("persisted")), null);
  await browser.close();
  // op1 may not close-and-take the running op2 profile
  assert.equal((await control("/profiles/open", { name: "alpha", owner: "op1" })).status, 409);
});

test("trash checks the owner, closes a running profile and keeps the files", async () => {
  assert.equal((await control("/profiles/trash", { name: "alpha", owner: "op1", reason: "deleted" })).status, 403);
  const trashed = await control("/profiles/trash", { name: "alpha", owner: "op2", reason: "deleted" });
  assert.equal(trashed.status, 200);
  assert.equal(trashed.body.was_open, true);
  assert.equal(existsSync(join(profilesRoot, "alpha")), false);
  assert.ok(existsSync(join(trashed.body.trash_path, "Default")));
  const missing = await control("/profiles/trash", { name: "alpha", owner: "op2" });
  assert.deepEqual(missing.body, { trashed: false, existed: false, was_open: false });
});

test("rename moves the on-disk profile and its logins to the new name", async () => {
  const opened = await control("/profiles/open", { name: "beta", owner: "op1" });
  const browser = await attach(opened.body.cdp_endpoint);
  const page = browser.contexts()[0].pages()[0] || (await browser.contexts()[0].newPage());
  await page.goto(`http://${LAN}:${CONTROL_PORT}/healthz`);
  await page.evaluate(() => localStorage.setItem("who", "beta"));
  await browser.close();

  assert.equal((await control("/profiles/rename", { name: "beta", new_name: "gamma", owner: "op9" })).status, 403);
  const renamed = await control("/profiles/rename", { name: "beta", new_name: "gamma", owner: "op1" });
  assert.equal(renamed.status, 200, JSON.stringify(renamed.body));
  assert.equal(renamed.body.renamed, true);
  assert.equal(existsSync(join(profilesRoot, "beta")), false);

  const reopened = await control("/profiles/open", { name: "gamma", owner: "op1" });
  assert.equal(reopened.body.was_empty, false);
  const b2 = await attach(reopened.body.cdp_endpoint);
  const p2 = b2.contexts()[0].pages()[0] || (await b2.contexts()[0].newPage());
  await p2.goto(`http://${LAN}:${CONTROL_PORT}/healthz`);
  assert.equal(await p2.evaluate(() => localStorage.getItem("who")), "beta");
  await b2.close();
  await closeLatest("gamma");
});

test("deep healthcheck launches, relays and attaches a disposable profile", async () => {
  assert.equal((await control("/healthz/deep", null, { token: null, method: "GET" })).status, 401);
  const deep = await control("/healthz/deep", null, { method: "GET" });
  assert.equal(deep.status, 200, JSON.stringify(deep.body));
  assert.equal(deep.body.ok, true);
  assert.deepEqual(readdirSync(join(profilesRoot, ".healthcheck")), []);
});

test("a close carrying an older generation cannot kill a newer open (Open/Close interleaving)", async () => {
  // Session A opens, starts closing; before its close reaches browser-node a
  // new Open of the same profile re-attaches to the still-running process.
  const a = await control("/profiles/open", { name: "delta", owner: "op1" });
  const b = await control("/profiles/open", { name: "delta", owner: "op1" });
  assert.equal(b.body.already_open, true);
  const browser = await attach(b.body.cdp_endpoint);
  const late = await control("/profiles/close", { name: "delta", generation: a.body.generation });
  assert.deepEqual(late.body, { closed: false, existed: true, stale_generation: true });
  // B's browser is still alive and usable.
  const page = browser.contexts()[0].pages()[0] || (await browser.contexts()[0].newPage());
  await page.goto(`http://${LAN}:${CONTROL_PORT}/healthz`);
  assert.match(await page.content(), /persistent_profiles_enabled/);
  await browser.close();
  const own = await control("/profiles/close", { name: "delta", generation: b.body.generation });
  assert.deepEqual(own.body, { closed: true, existed: true });
});

test("an unreadable owner marker is never opened: moved to trash, fresh profile", async () => {
  const dir = join(profilesRoot, "epsilon");
  mkdirSync(join(dir, "Default"), { recursive: true });
  writeFileSync(join(dir, "Default", "secret-login"), "alice's cookies");
  writeFileSync(join(dir, ".auto-browser-owner.json"), "{not json");
  const opened = await control("/profiles/open", { name: "epsilon", owner: "mallory" });
  assert.equal(opened.status, 200, JSON.stringify(opened.body));
  assert.equal(opened.body.was_empty, true);
  assert.equal(existsSync(join(dir, "Default", "secret-login")), false);
  const trashed = readdirSync(join(profilesRoot, ".trash")).filter((n) => n.startsWith("epsilon--"));
  assert.ok(trashed.some((n) => n.endsWith("--bad-marker")), trashed.join(","));
  await closeLatest("epsilon");
  // A bad marker also blocks rename (never moves unknown logins to a new name).
  writeFileSync(join(dir, ".auto-browser-owner.json"), "[]");
  assert.equal((await control("/profiles/rename", { name: "epsilon", new_name: "epsilon2", owner: "mallory" })).status, 409);
});

test("unmarked existing data: only an adopting (remember-me) open keeps it", async () => {
  for (const name of ["zeta", "eta"]) {
    mkdirSync(join(profilesRoot, name, "Default"), { recursive: true });
    writeFileSync(join(profilesRoot, name, "Default", "old-data"), "x");
  }
  const named = await control("/profiles/open", { name: "zeta", owner: "op1" });
  assert.equal(named.body.was_empty, true);
  assert.ok(readdirSync(join(profilesRoot, ".trash")).some((n) => n.startsWith("zeta--") && n.endsWith("--unmarked")));
  await closeLatest("zeta");

  const adopted = await control("/profiles/open", { name: "eta", owner: "op1", adopt_unmarked: true });
  assert.equal(adopted.body.was_empty, false);
  assert.ok(existsSync(join(profilesRoot, "eta", "Default", "old-data")));
  const marker = JSON.parse(readFileSync(join(profilesRoot, "eta", ".auto-browser-owner.json"), "utf-8"));
  assert.equal(marker.owner, "op1");
  await closeLatest("eta");
});

test("node lease: a second browser-node on the same volume refuses to launch (fail closed)", async () => {
  const serverPath = fileURLToPath(new URL("../server.mjs", import.meta.url));
  const second = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      PERSISTENT_PROFILES_ENABLED: "true",
      PROFILE_CONTROL_TOKEN: TOKEN,
      PROFILE_CONTROL_PORT: String(CONTROL_PORT + 100),
      PROFILE_CDP_RELAY_PORT: String(RELAY_PORT + 100),
      PLAYWRIGHT_SERVER_HOST: "127.0.0.1",
      PLAYWRIGHT_SERVER_PORT: String(LEGACY_PORT + 100),
      BROWSER_WS_ENDPOINT_FILE: join(root, "profile2", "ws.txt"),
      BROWSER_PROFILES_ROOT: profilesRoot,
      BROWSER_DOWNLOADS_DIR: join(root, "downloads2"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let secondLog = "";
  second.stdout.on("data", (c) => (secondLog += c));
  second.stderr.on("data", (c) => (secondLog += c));
  try {
    for (let i = 0; i < 120 && !existsSync(join(root, "profile2", "ws.txt")); i += 1) {
      await new Promise((r) => setTimeout(r, 250));
    }
    const refused = await fetch(`http://${LAN}:${CONTROL_PORT + 100}/profiles/open`, {
      method: "POST",
      headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({ name: "theta", owner: "op1" }),
    });
    assert.equal(refused.status, 503);
    assert.match(secondLog, /refusing to launch or unlock any profile/);
    assert.equal(existsSync(join(profilesRoot, "theta")), false);
    // The first node is unaffected.
    const ok = await control("/profiles/open", { name: "theta", owner: "op1" });
    assert.equal(ok.status, 200);
    await closeLatest("theta");
  } finally {
    second.kill();
  }
});

test("node lease: a fresh foreign lease blocks; a stale one is taken over", async () => {
  const leaseFile = join(profilesRoot, ".node-lease.json");
  writeFileSync(leaseFile, JSON.stringify({ node_id: "other-container:1:abcd", heartbeat_at: Date.now() }));
  const blocked = await control("/profiles/open", { name: "iota", owner: "op1" });
  assert.equal(blocked.status, 503, JSON.stringify(blocked.body));
  writeFileSync(leaseFile, JSON.stringify({ node_id: "other-container:1:abcd", heartbeat_at: Date.now() - 120_000 }));
  const taken = await control("/profiles/open", { name: "iota", owner: "op1" });
  assert.equal(taken.status, 200, JSON.stringify(taken.body));
  assert.notEqual(JSON.parse(readFileSync(leaseFile, "utf-8")).node_id, "other-container:1:abcd");
  await closeLatest("iota");
});
