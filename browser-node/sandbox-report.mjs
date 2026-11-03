// Prints how Chromium runs in this container, measured now: the state server.mjs wrote, a live /proc check of the
// renderers (own user namespace, Chromium's seccomp filter on top of the container's) and chrome://sandbox.
// Informational: it exits 0 whatever the state, so BROWSER_SANDBOX=auto's fallback keeps a smoke green.
//   docker exec -u browser <container> node /opt/browser-node/sandbox-report.mjs
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { chromium } from "playwright";

import { realProc, verifySandbox } from "./sandbox.mjs";

const endpointFile = process.env.BROWSER_WS_ENDPOINT_FILE || "/data/profile/browser-ws-endpoint.txt";
const stateFile = process.env.BROWSER_SANDBOX_STATE_FILE || join(dirname(endpointFile), "browser-sandbox.json");
const label = process.argv[2] || "this container";

let state = "missing";
try {
  state = readFileSync(stateFile, "utf-8").trim();
} catch {
  // An image older than 1.14.0, or a server that has not started.
}
console.log(`[${label}] state file: ${state}`);

function mainBrowserPid() {
  const chromes = [];
  for (const pid of realProc.pids()) {
    try {
      const argv = realProc.cmdline(pid);
      if (argv.length && /chrome$/.test(argv[0]) && !argv.some((arg) => arg.startsWith("--type="))) chromes.push(pid);
    } catch {
      // Exited.
    }
  }
  // The browser process is the one whose parent is not itself a Chromium process.
  return chromes.find((pid) => !chromes.includes(String(realProc.ppid(pid))));
}

let client;
try {
  const endpoint = readFileSync(endpointFile, "utf-8").trim().replace(/\/\/[^/:]+:/, "//127.0.0.1:");
  client = await chromium.connect(endpoint, { timeout: 30000 });
  const page = await client.newPage();
  await page.setContent("<p>sandbox report</p>");
  const browserPid = mainBrowserPid();
  const live = verifySandbox({ browserPid });
  console.log(`[${label}] live check (browser pid ${browserPid}): ${JSON.stringify(live)}`);
  try {
    await page.goto("chrome://sandbox", { timeout: 15000 });
    await page.waitForTimeout(1000);
    const rows = (await page.innerText("body"))
      .split("\n")
      .map((line) => line.trim().replace(/\t+/g, ": "))
      .filter(Boolean);
    console.log(`[${label}] chrome://sandbox: ${rows.join(" | ")}`);
  } catch (error) {
    console.log(`[${label}] chrome://sandbox not reachable: ${String(error).split("\n")[0]}`);
  }
} catch (error) {
  console.log(`[${label}] live check failed: ${String(error).split("\n")[0]}`);
} finally {
  await client?.close().catch(() => {});
}
