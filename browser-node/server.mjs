import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { chromium } from "playwright";

import {
  declaredProfile,
  diagnose,
  launchWithSandboxPolicy,
  parseSetting,
  SandboxRefusedError,
  makeVerify,
  writeState,
} from "./sandbox.mjs";

const width = Number.parseInt(process.env.BROWSER_WIDTH || "1280", 10);
const height = Number.parseInt(process.env.BROWSER_HEIGHT || "800", 10);
const endpointFile = process.env.BROWSER_WS_ENDPOINT_FILE || "/data/profile/browser-ws-endpoint.txt";
const host = process.env.PLAYWRIGHT_SERVER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.PLAYWRIGHT_SERVER_PORT || "9223", 10);
const advertisedHost = process.env.PLAYWRIGHT_SERVER_ADVERTISED_HOST || "browser-node";
// Written before the endpoint file, so a reader that sees the endpoint also sees how Chromium runs.
const sandboxStateFile = process.env.BROWSER_SANDBOX_STATE_FILE || join(dirname(endpointFile), "browser-sandbox.json");
const sandboxSetting = parseSetting(process.env.BROWSER_SANDBOX);
const seccompProfile = declaredProfile(process.env.BROWSER_SECCOMP_PROFILE);

// Playwright's own --disable-features list (driver coreBundle.js `disabledFeatures`) without
// ThirdPartyStoragePartitioning. Chromium keeps the last --disable-features switch, so this turns storage
// partitioning back on, as in real Chrome, and keeps everything else Playwright disables: unpartitioned, a refused
// host's iframe could register a service worker that serves a top-level navigation the pre-request gate never sees.
// The controller's copy is app/browser/launch_flags.py; tests/test_playwright_disabled_features.py checks both.
const PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING = [
  "AvoidUnnecessaryBeforeUnloadCheckSync",
  "DestroyProfileOnBrowserClose",
  "DialMediaRouteProvider",
  "GlobalMediaControls",
  "HttpsUpgrades",
  "LensOverlay",
  "MediaRouter",
  "PaintHolding",
  "BlockOriginHeaderModificationOnRedirect",
  "AvoidCorsURLLoaderRestartOnRedirect",
  "Translate",
  "AutoDeElevate",
  "OptimizationHints",
  "NetworkTimeServiceQuerying",
  "AimEnabled",
  "msForceBrowserSignIn",
  "msEdgeUpdateLaunchServicesPreferredVersion",
];

const chromiumArgs = [
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
  `--disable-features=${PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING.join(",")}`,
];

// BROWSER_SANDBOX (sandbox.mjs): auto tries Chromium's sandbox and falls back with a WARNING, on refuses to run
// without it, off is the old unsandboxed launch.
const launch = (sandboxed) =>
  chromium.launchServer({
    headless: false,
    chromiumSandbox: sandboxed,
    host,
    port,
    downloadsPath: "/data/downloads",
    args: chromiumArgs,
  });

// After a sandboxed launch: sandbox.mjs verifyServer (through makeVerify) opens one page through the server and checks
// its renderers in /proc (and chrome://sandbox on kernels without Seccomp_filters), each step time-bounded.
const verify = makeVerify(chromium);

// The endpoint the controller is told (the endpoint file) and the one the state names: the same string.
function advertised(server) {
  const endpoint = new URL(server.wsEndpoint());
  endpoint.hostname = advertisedHost;
  endpoint.port = String(port);
  return endpoint.toString();
}

await mkdir(dirname(sandboxStateFile), { recursive: true });
let browserServer;
try {
  const result = await launchWithSandboxPolicy({
    setting: sandboxSetting,
    profile: seccompProfile,
    launch,
    verify,
    diagnose,
    log: console,
  });
  browserServer = result.server;
  // The state names this run's endpoint, so the controller can tell it from a previous run's (or a rolled-back image's).
  await writeState(sandboxStateFile, { ...result.state, endpoint: advertised(browserServer) });
  console.log(`chromium sandbox: ${result.state.sandbox} (${result.state.cause ?? "verified"}); state in ${sandboxStateFile}`);
} catch (error) {
  if (error instanceof SandboxRefusedError) {
    await writeState(sandboxStateFile, error.state);
    console.error(error.message);
    process.exit(1);
  }
  throw error;
}

const advertisedEndpoint = advertised(browserServer);

await mkdir(dirname(endpointFile), { recursive: true });
const tmpFile = `${endpointFile}.tmp`;
await writeFile(tmpFile, advertisedEndpoint, "utf-8");
await rename(tmpFile, endpointFile);
console.log(`wrote ${endpointFile}: ${advertisedEndpoint}`);

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    await browserServer.close();
    process.exit(0);
  });
}

await new Promise((resolve) => browserServer.on("close", resolve));
