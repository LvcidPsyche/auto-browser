import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { chromium } from "playwright";

const width = Number.parseInt(process.env.BROWSER_WIDTH || "1280", 10);
const height = Number.parseInt(process.env.BROWSER_HEIGHT || "800", 10);
const endpointFile = process.env.BROWSER_WS_ENDPOINT_FILE || "/data/profile/browser-ws-endpoint.txt";
const host = process.env.PLAYWRIGHT_SERVER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.PLAYWRIGHT_SERVER_PORT || "9223", 10);
const advertisedHost = process.env.PLAYWRIGHT_SERVER_ADVERTISED_HOST || "browser-node";

const browserServer = await chromium.launchServer({
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

const rawEndpoint = new URL(browserServer.wsEndpoint());
rawEndpoint.hostname = advertisedHost;
rawEndpoint.port = String(port);
const advertisedEndpoint = rawEndpoint.toString();

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
