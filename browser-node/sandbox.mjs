// Chromium's own sandbox in browser-node: BROWSER_SANDBOX=auto|on|off (default auto).
//
// The sandbox's first layer is a user namespace (with PID and network namespaces) that each renderer is chrooted
// into; the second is Chromium's seccomp-BPF filter. Docker's default seccomp profile refuses the namespace calls
// without CAP_SYS_ADMIN, which these containers drop, so compose and the controller apply the profile in
// controller/app/browser/seccomp/ (moby's default plus clone, unshare and chroot). Where that is not enough (a
// container started without the profile, or a host whose kernel or AppArmor refuses unprivileged user namespaces,
// such as Ubuntu 23.10+), `auto` falls back to an unsandboxed Chromium and says why, `on` refuses to start, and
// `off` never tries. The state goes to a JSON file beside the endpoint file, which the controller's readiness reads.
import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync, readlinkSync } from "node:fs";
import { rename, writeFile } from "node:fs/promises";

const SETTINGS = ["auto", "on", "off"];

export function parseSetting(raw) {
  const value = String(raw ?? "").trim().toLowerCase() || "auto";
  if (!SETTINGS.includes(value)) {
    throw new Error(`BROWSER_SANDBOX must be auto, on or off, got '${raw}'`);
  }
  return value;
}

// What the container's creator says it applied (compose and the controller set BROWSER_SECCOMP_PROFILE). Only a
// hint for naming a cause: the probe below is the evidence.
export function declaredProfile(raw) {
  const value = String(raw ?? "").trim();
  if (!value || value === "builtin") return "builtin";
  if (value === "unconfined" || value === "auto-browser") return value;
  return "custom";
}

// Tries exactly what the sandbox needs: a user namespace as this unprivileged user, and chroot inside it.
export function probeNamespaces() {
  const result = spawnSync("unshare", ["--user", "--map-root-user", "chroot", "/", "true"], {
    encoding: "utf-8",
    timeout: 5000,
  });
  if (result.error) {
    return { ok: false, stage: "unavailable", stderr: String(result.error.message || result.error) };
  }
  const stderr = (result.stderr || "").trim();
  if (result.status === 0) return { ok: true, stage: null, stderr };
  return { ok: false, stage: /^chroot:/m.test(stderr) ? "chroot" : "unshare", stderr };
}

function readSysctl(path) {
  try {
    return readFileSync(path, "utf-8").trim();
  } catch {
    return undefined;
  }
}

export function kernelFacts() {
  return {
    apparmor_restrict_unprivileged_userns: readSysctl("/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
    max_user_namespaces: readSysctl("/proc/sys/user/max_user_namespaces"),
    unprivileged_userns_clone: readSysctl("/proc/sys/kernel/unprivileged_userns_clone"),
  };
}

export function diagnose() {
  return { probe: probeNamespaces(), kernel: kernelFacts() };
}

function chromiumReason(error) {
  const text = String(error?.message ?? error ?? "");
  const fatal = text.split("\n").find((line) => line.includes(":FATAL:"));
  if (fatal) return fatal.replace(/^.*?:FATAL:[^\]]*\]\s*/, "").slice(0, 300);
  return text.split("\n")[0].slice(0, 300);
}

// One cause per failure, worded so a missing profile never reads like a host that refuses namespaces.
export function classifyFailure({ probe, kernel = {}, profile, chromiumError }) {
  const chromium = chromiumReason(chromiumError);
  if (probe.ok) {
    return {
      cause: "chromium_refused",
      detail:
        "user namespaces and chroot work in this container, but Chromium still could not start sandboxed: " +
        chromium,
    };
  }
  if (probe.stage === "unavailable") {
    return {
      cause: "probe_unavailable",
      detail: `Chromium could not start sandboxed (${chromium}), and the namespace probe could not run: ${probe.stderr}`,
    };
  }
  if (profile === "builtin") {
    return {
      cause: "seccomp_profile_not_applied",
      detail:
        "the auto-browser seccomp profile is not applied to this container, and Docker's default profile refuses " +
        `the user namespace Chromium's sandbox needs (${probe.stderr}). Start it with docker compose or from the ` +
        "controller, or pass --security-opt seccomp=controller/app/browser/seccomp/chromium-sandbox.json.",
    };
  }
  if (probe.stage === "chroot") {
    return {
      cause: "chroot_refused",
      detail:
        "a user namespace can be created, but chroot inside it is refused " +
        `(${probe.stderr}); the applied seccomp profile does not allow chroot.`,
    };
  }
  const hints = [];
  if (kernel.apparmor_restrict_unprivileged_userns === "1") {
    hints.push("kernel.apparmor_restrict_unprivileged_userns=1 (AppArmor's restriction, Ubuntu 23.10 and later)");
  }
  if (kernel.max_user_namespaces === "0") hints.push("user.max_user_namespaces=0");
  if (kernel.unprivileged_userns_clone === "0") hints.push("kernel.unprivileged_userns_clone=0");
  return {
    cause: "userns_refused",
    detail:
      `the kernel or AppArmor refused to create a user namespace although the seccomp profile (${profile}) allows ` +
      `it (${probe.stderr})` +
      (hints.length ? `; host setting: ${hints.join(", ")}` : "") +
      ".",
  };
}

export class SandboxRefusedError extends Error {
  constructor(message, state) {
    super(message);
    this.state = state;
  }
}

export async function launchWithSandboxPolicy({ setting, profile, launch, verify, diagnose: runDiagnose, log }) {
  const base = { setting, seccomp_profile: profile };
  if (setting === "off") {
    const server = await launch(false);
    const exposed = runDiagnose().probe.ok;
    let detail = "BROWSER_SANDBOX=off: Chromium runs without its sandbox.";
    if (exposed) {
      detail +=
        " This container still allows user namespaces (a seccomp profile admitting them is applied); start it" +
        " with Docker's default profile (under compose, BROWSER_SECCOMP_PROFILE=builtin) for the narrower kernel" +
        " surface.";
      log.warn(`WARNING: ${detail}`);
    }
    return { server, state: { ...base, sandbox: "off", cause: "setting_off", detail, userns_exposed: exposed } };
  }

  let failure;
  try {
    const server = await launch(true);
    let verdict;
    try {
      verdict = await verify(server);
    } catch (error) {
      verdict = { ok: false, detail: `the sandbox check failed: ${String(error?.message ?? error).split("\n")[0]}` };
    }
    if (verdict.ok) {
      return {
        server,
        state: {
          ...base,
          sandbox: "on",
          cause: null,
          detail: "Chromium runs sandboxed: renderers are in their own user namespace under Chromium's seccomp filter.",
          verified: verdict.evidence,
        },
      };
    }
    await server.close().catch(() => {});
    failure = { cause: "not_verified", detail: `Chromium launched with its sandbox requested, but ${verdict.detail}` };
  } catch (error) {
    failure = classifyFailure({ ...runDiagnose(), profile, chromiumError: error });
  }

  if (setting === "on") {
    throw new SandboxRefusedError(
      `BROWSER_SANDBOX=on: Chromium could not start sandboxed, and an unsandboxed start is refused. ` +
        `Cause (${failure.cause}): ${failure.detail}`,
      { ...base, sandbox: "refused", ...failure },
    );
  }
  log.warn(
    `WARNING: Chromium's sandbox is OFF in this container (BROWSER_SANDBOX=auto fell back to an unsandboxed ` +
      `Chromium). Cause (${failure.cause}): ${failure.detail}`,
  );
  const server = await launch(false);
  return { server, state: { ...base, sandbox: "off", ...failure } };
}

// /proc access, injectable for tests.
export const realProc = {
  pids: () => readdirSync("/proc").filter((entry) => /^\d+$/.test(entry)),
  cmdline: (pid) => {
    const parts = readFileSync(`/proc/${pid}/cmdline`, "utf-8").split("\0").filter(Boolean);
    // Chromium rewrites its child processes' argv into one space-separated string.
    return parts.length === 1 ? parts[0].split(" ").filter(Boolean) : parts;
  },
  ppid: (pid) => readFileSync(`/proc/${pid}/stat`, "utf-8").replace(/^.*\)\s+\S+\s+/, "").split(/\s+/)[0],
  status: (pid) => readFileSync(`/proc/${pid}/status`, "utf-8"),
  userns: (pid) => readlinkSync(`/proc/${pid}/ns/user`),
};

function seccompFilters(status) {
  const match = status.match(/^Seccomp_filters:\s*(\d+)/m);
  return match ? Number(match[1]) : undefined;
}

// Proof from /proc, not from the flag: every renderer under this browser runs in a user namespace other than the
// browser's and carries more seccomp filters than it (Chromium's on top of the container's).
export function verifySandbox({ browserPid, proc = realProc }) {
  const browser = String(browserPid);
  const parents = new Map();
  for (const pid of proc.pids()) {
    try {
      parents.set(String(pid), String(proc.ppid(pid)));
    } catch {
      // The process exited while we looked.
    }
  }
  const descends = (pid) => {
    for (let current = parents.get(pid), hops = 0; current && hops < 64; current = parents.get(current), hops += 1) {
      if (current === browser) return true;
    }
    return false;
  };
  const renderers = [];
  for (const pid of parents.keys()) {
    try {
      if (descends(pid) && proc.cmdline(pid).join(" ").split(" ").includes("--type=renderer")) renderers.push(pid);
    } catch {
      // Exited.
    }
  }
  if (!renderers.length) return { ok: false, detail: "no renderer process was found under the browser to check" };
  const browserNs = proc.userns(browser);
  const browserFilters = seccompFilters(proc.status(browser));
  const rendererFilters = renderers.map((pid) => seccompFilters(proc.status(pid)));
  const userns = renderers.every((pid) => proc.userns(pid) !== browserNs);
  if (!userns) return { ok: false, detail: "a renderer runs in the browser's own user namespace" };
  if (browserFilters === undefined || !rendererFilters.every((count) => count > browserFilters)) {
    return {
      ok: false,
      detail: `a renderer has no seccomp filter beyond the browser's (${rendererFilters} vs ${browserFilters})`,
    };
  }
  return {
    ok: true,
    evidence: {
      renderers: renderers.length,
      renderer_userns_differs: true,
      browser_seccomp_filters: browserFilters,
      renderer_seccomp_filters: rendererFilters,
    },
  };
}

export async function writeState(path, state) {
  const tmp = `${path}.tmp`;
  await writeFile(tmp, `${JSON.stringify(state)}\n`, "utf-8");
  await rename(tmp, path);
}
