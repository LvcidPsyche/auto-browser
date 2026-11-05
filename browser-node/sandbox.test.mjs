// node --test browser-node/  (CI runs it; locally: inside the browser-node image, see scripts in REPORT)
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  classifyFailure,
  declaredProfile,
  FALLBACK_LAUNCH_TIMEOUT_MS,
  SANDBOX_PHASE_BUDGET_MS,
  SandboxRefusedError,
  launchWithSandboxPolicy,
  makeVerify,
  verifyServer,
  parseSetting,
  redactEndpoints,
  verifySandbox,
} from "./sandbox.mjs";

const NAMESPACE_OK = { probe: { ok: true, stage: null, stderr: "" }, kernel: {} };
const UNSHARE_REFUSED = {
  probe: { ok: false, stage: "unshare", stderr: "unshare: unshare failed: Operation not permitted" },
  kernel: {},
};
const CHROOT_REFUSED = {
  probe: { ok: false, stage: "chroot", stderr: "chroot: cannot change root directory to '/': Operation not permitted" },
  kernel: {},
};
const CHROMIUM_FATAL = new Error(
  "Failed to launch browser.\n[pid=92][err] [92:92:1103/180119.513726:FATAL:content/browser/zygote_host/" +
    "zygote_host_impl_linux.cc:130] No usable sandbox! If you are running on Ubuntu 23.10+ ...",
);

function fakeServer(name) {
  return { name, closed: false, async close() { this.closed = true; } };
}

function harness({ sandboxedLaunch = "ok", verified = true, diagnosis = NAMESPACE_OK } = {}) {
  const launches = [];
  const warnings = [];
  return {
    launches,
    warnings,
    log: { warn: (message) => warnings.push(message), info: () => {} },
    async launch(sandboxed) {
      launches.push(sandboxed);
      if (sandboxed && sandboxedLaunch === "throws") throw CHROMIUM_FATAL;
      return fakeServer(sandboxed ? "sandboxed" : "unsandboxed");
    },
    async verify() {
      return verified
        ? { ok: true, evidence: { renderers: 2, renderer_userns_differs: true } }
        : { ok: false, detail: "no renderer runs in its own user namespace" };
    },
    diagnose: () => diagnosis,
  };
}

test("the setting defaults to auto and accepts only auto, on and off", () => {
  assert.equal(parseSetting(undefined), "auto");
  assert.equal(parseSetting(""), "auto");
  assert.equal(parseSetting(" On "), "on");
  assert.equal(parseSetting("off"), "off");
  assert.throws(() => parseSetting("yes"), /BROWSER_SANDBOX must be auto, on or off, got 'yes'/);
});

test("the declared profile tells the auto-browser profile from Docker's default", () => {
  assert.equal(declaredProfile(undefined), "builtin");
  assert.equal(declaredProfile(""), "builtin");
  assert.equal(declaredProfile("builtin"), "builtin");
  assert.equal(declaredProfile("auto-browser"), "auto-browser");
  assert.equal(declaredProfile("unconfined"), "unconfined");
  assert.equal(declaredProfile("./my/profile.json"), "custom");
});

test("auto: a sandboxed launch that verifies is kept, sandbox on", async () => {
  const h = harness();
  const { server, state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h });
  assert.deepEqual(h.launches, [true]);
  assert.equal(server.name, "sandboxed");
  assert.equal(state.sandbox, "on");
  assert.equal(state.cause, null);
  assert.deepEqual(state.verified, { renderers: 2, renderer_userns_differs: true });
  assert.deepEqual(h.warnings, []);
});

test("auto: a refused sandboxed launch relaunches unsandboxed with a WARNING naming the cause", async () => {
  const h = harness({ sandboxedLaunch: "throws", diagnosis: UNSHARE_REFUSED });
  const { server, state } = await launchWithSandboxPolicy({ setting: "auto", profile: "builtin", ...h });
  assert.deepEqual(h.launches, [true, true, false]);
  assert.equal(server.name, "unsandboxed");
  assert.equal(state.sandbox, "off");
  assert.equal(state.cause, "seccomp_profile_not_applied");
  assert.equal(h.warnings.length, 2); // the failed first attempt, then the fallback
  assert.match(h.warnings[1], /^WARNING: Chromium's sandbox is OFF/);
  assert.match(h.warnings[1], /seccomp_profile_not_applied/);
  assert.match(h.warnings[1], /not applied/);
});

test("auto: a launch whose renderers are not actually sandboxed is closed and relaunched unsandboxed", async () => {
  const h = harness({ verified: false });
  const { server, state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h });
  assert.deepEqual(h.launches, [true, true, false]);
  assert.equal(server.name, "unsandboxed");
  assert.equal(state.sandbox, "off");
  assert.equal(state.cause, "not_verified");
  assert.match(state.detail, /no renderer runs in its own user namespace/);
  assert.equal(h.warnings.length, 2);
});

test("on: a refused sandboxed launch is an error, and nothing is launched unsandboxed", async () => {
  const h = harness({ sandboxedLaunch: "throws", diagnosis: UNSHARE_REFUSED });
  await assert.rejects(
    launchWithSandboxPolicy({ setting: "on", profile: "auto-browser", ...h }),
    (error) => {
      assert.match(error.message, /BROWSER_SANDBOX=on/);
      assert.match(error.message, /userns_refused/);
      assert.equal(error.state.sandbox, "refused");
      return true;
    },
  );
  assert.deepEqual(h.launches, [true, true]);
});

test("on: an unverified sandbox is refused too", async () => {
  const h = harness({ verified: false });
  await assert.rejects(launchWithSandboxPolicy({ setting: "on", profile: "auto-browser", ...h }), /not_verified/);
  assert.deepEqual(h.launches, [true, true]);
});

test("off: today's behaviour, one unsandboxed launch, and the state says why", async () => {
  const h = harness({ diagnosis: UNSHARE_REFUSED });
  const { server, state } = await launchWithSandboxPolicy({ setting: "off", profile: "builtin", ...h });
  assert.deepEqual(h.launches, [false]);
  assert.equal(server.name, "unsandboxed");
  assert.equal(state.sandbox, "off");
  assert.equal(state.cause, "setting_off");
  assert.equal(state.userns_exposed, false);
  assert.deepEqual(h.warnings, []);
});

test("off: a container that still allows user namespaces is called out", async () => {
  const h = harness({ diagnosis: NAMESPACE_OK });
  const { state } = await launchWithSandboxPolicy({ setting: "off", profile: "auto-browser", ...h });
  assert.equal(state.userns_exposed, true);
  assert.match(state.detail, /BROWSER_SECCOMP_PROFILE=builtin/);
  assert.equal(h.warnings.length, 1);
});

test("causes: the profile not applied reads differently from a kernel or AppArmor refusal", () => {
  const notApplied = classifyFailure({ ...UNSHARE_REFUSED, profile: "builtin", chromiumError: CHROMIUM_FATAL });
  assert.equal(notApplied.cause, "seccomp_profile_not_applied");
  assert.match(notApplied.detail, /auto-browser seccomp profile is not applied/);

  const refused = classifyFailure({
    probe: UNSHARE_REFUSED.probe,
    kernel: { apparmor_restrict_unprivileged_userns: "1" },
    profile: "auto-browser",
    chromiumError: CHROMIUM_FATAL,
  });
  assert.equal(refused.cause, "userns_refused");
  assert.match(refused.detail, /kernel or AppArmor refused/);
  assert.match(refused.detail, /apparmor_restrict_unprivileged_userns=1/);
  assert.doesNotMatch(refused.detail, /not applied/);

  const maxZero = classifyFailure({
    probe: UNSHARE_REFUSED.probe,
    kernel: { max_user_namespaces: "0" },
    profile: "auto-browser",
    chromiumError: CHROMIUM_FATAL,
  });
  assert.match(maxZero.detail, /user\.max_user_namespaces=0/);
});

test("causes: chroot refused inside the namespace, and Chromium refusing on its own", () => {
  const chroot = classifyFailure({ ...CHROOT_REFUSED, profile: "custom", chromiumError: CHROMIUM_FATAL });
  assert.equal(chroot.cause, "chroot_refused");
  assert.match(chroot.detail, /chroot/);

  const chromium = classifyFailure({ ...NAMESPACE_OK, profile: "auto-browser", chromiumError: CHROMIUM_FATAL });
  assert.equal(chromium.cause, "chromium_refused");
  assert.match(chromium.detail, /No usable sandbox!/);
});

function fakeProc(table) {
  // table: pid -> { ppid, cmdline, userns, filters }
  return {
    pids: () => Object.keys(table),
    cmdline: (pid) => table[pid].cmdline,
    ppid: (pid) => table[pid].ppid,
    status: (pid) => `Name:\tchrome\nSeccomp:\t2\nSeccomp_filters:\t${table[pid].filters}\n`,
    userns: (pid) => `user:[${table[pid].userns}]`,
  };
}

test("verification: renderers in their own user namespace, with one more seccomp filter", () => {
  const proc = fakeProc({
    10: { ppid: 1, cmdline: ["/ms-playwright/chrome"], userns: 1, filters: 2 },
    11: { ppid: 10, cmdline: ["/ms-playwright/chrome --type=zygote"], userns: 2, filters: 2 },
    12: { ppid: 11, cmdline: ["/ms-playwright/chrome --type=renderer"], userns: 2, filters: 3 },
    99: { ppid: 1, cmdline: ["/other/chrome --type=renderer"], userns: 1, filters: 2 },
  });
  const result = verifySandbox({ browserPid: "10", proc });
  assert.equal(result.ok, true);
  assert.deepEqual(result.evidence, {
    renderers: 1,
    renderer_userns_differs: true,
    browser_seccomp_filters: 2,
    renderer_seccomp_filters: [3],
  });
});

test("verification fails for renderers that share the browser's namespace or filters, or when none exists", () => {
  const sameNs = fakeProc({
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer --no-sandbox"], userns: 1, filters: 2 },
  });
  const failed = verifySandbox({ browserPid: "10", proc: sameNs });
  assert.equal(failed.ok, false);
  assert.match(failed.detail, /user namespace/);

  const sameFilters = fakeProc({
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2, filters: 2 },
  });
  assert.match(verifySandbox({ browserPid: "10", proc: sameFilters }).detail, /seccomp/);

  const none = fakeProc({ 10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 } });
  assert.match(verifySandbox({ browserPid: "10", proc: none }).detail, /no renderer/);
});

// ---- round 2 ----

test("verification skips a renderer that exited mid-check and needs one still alive", () => {
  const table = {
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2, filters: 3 },
    13: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2, filters: 3 },
  };
  const proc = fakeProc(table);
  const gone = (pid) => {
    const error = new Error(`ENOENT: /proc/${pid}/status`);
    error.code = "ENOENT";
    return error;
  };
  const exitedOne = { ...proc, status: (pid) => { if (pid === "13") throw gone(pid); return proc.status(pid); } };
  const ok = verifySandbox({ browserPid: "10", proc: exitedOne });
  assert.equal(ok.ok, true);
  assert.equal(ok.evidence.renderers, 1);

  const allGone = { ...proc, userns: (pid) => { if (pid !== "10") throw gone(pid); return proc.userns(pid); } };
  const none = verifySandbox({ browserPid: "10", proc: allGone });
  assert.equal(none.ok, false);
  assert.match(none.detail, /no renderer/);
});

test("verification without Seccomp_filters (kernel < 5.9) uses chrome://sandbox, or says it cannot verify", () => {
  const table = {
    10: { ppid: 1, cmdline: ["chrome"], userns: 1 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2 },
  };
  const proc = { ...fakeProc(table), status: () => "Name:\tchrome\nSeccomp:\t2\n" };
  const confirmed = verifySandbox({
    browserPid: "10",
    proc,
    sandboxPage: "Layer 1 Sandbox\tNamespace\nSeccomp-BPF sandbox\tYes\nYou are adequately sandboxed.",
  });
  assert.equal(confirmed.ok, true);
  assert.equal(confirmed.evidence.method, "chrome://sandbox");
  const unknown = verifySandbox({ browserPid: "10", proc });
  assert.equal(unknown.ok, false);
  assert.equal(unknown.cause, "verification_unsupported");
  const denied = verifySandbox({ browserPid: "10", proc, sandboxPage: "Seccomp-BPF sandbox\tNo" });
  assert.equal(denied.cause, "verification_unsupported");
});

test("verifyServer opens a page through the server and checks it in /proc (what server.mjs runs)", async () => {
  const events = [];
  const server = { wsEndpoint: () => "ws://x", process: () => ({ pid: 10 }) };
  const page = {
    setContent: async () => events.push("setContent"),
    goto: async (url) => events.push(`goto ${url}`),
    innerText: async () => "Seccomp-BPF sandbox\tYes",
  };
  const client = { newPage: async () => page, close: async () => events.push("close") };
  const proc = fakeProc({
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2, filters: 3 },
  });
  const verdict = await verifyServer(server, { connect: async () => client, proc });
  assert.equal(verdict.ok, true);
  assert.deepEqual(events, ["setContent", "close"]);
  const unsandboxed = fakeProc({
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer --no-sandbox"], userns: 1, filters: 2 },
  });
  assert.equal((await verifyServer(server, { connect: async () => client, proc: unsandboxed })).ok, false);
});

test("auto retries the sandboxed launch once before falling back", async () => {
  let attempts = 0;
  const h = harness();
  h.verify = async () => {
    attempts += 1;
    return attempts === 1 ? { ok: false, detail: "a transient failure" } : { ok: true, evidence: { renderers: 1 } };
  };
  const { state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h });
  assert.equal(state.sandbox, "on");
  assert.deepEqual(h.launches, [true, true]);
  assert.deepEqual(h.warnings.length, 1); // the first attempt's failure, logged
});

test("a cause from verification is kept (verification_unsupported)", async () => {
  const h = harness();
  h.verify = async () => ({ ok: false, cause: "verification_unsupported", detail: "no Seccomp_filters" });
  const { state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h });
  assert.equal(state.cause, "verification_unsupported");
  assert.deepEqual(h.launches, [true, true, false]);
});

test("builtin on an AppArmor-restricted host names both possible causes", () => {
  const both = classifyFailure({
    probe: UNSHARE_REFUSED.probe,
    kernel: { apparmor_restrict_unprivileged_userns: "1" },
    profile: "builtin",
    chromiumError: CHROMIUM_FATAL,
  });
  assert.equal(both.cause, "seccomp_profile_not_applied");
  assert.match(both.detail, /Or AppArmor refused it/);
});

test("a daemon without seccomp support is its own cause", () => {
  assert.equal(declaredProfile("no-seccomp"), "no-seccomp");
  const failure = classifyFailure({ ...UNSHARE_REFUSED, profile: "no-seccomp", chromiumError: CHROMIUM_FATAL });
  assert.equal(failure.cause, "seccomp_unsupported");
});

// ---- round 3 ----

function fakePage(sandboxText, events) {
  return {
    setContent: async (_html, options) => events.push(`setContent ${options?.timeout}`),
    goto: async (url, options) => events.push(`goto ${url} ${options?.timeout}`),
    waitForFunction: async () => events.push("waitForFunction"),
    innerText: async () => sandboxText,
  };
}

test("makeVerify: what server.mjs uses gives ok:false for an unsandboxed tree", async () => {
  const events = [];
  const chromium = {
    connect: async (endpoint, options) => {
      events.push(`connect ${endpoint} ${options.timeout}`);
      return { newPage: async () => fakePage("", events), close: async () => events.push("close") };
    },
  };
  const server = { wsEndpoint: () => "ws://local", process: () => ({ pid: 10 }) };
  const unsandboxed = fakeProc({
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 1, filters: 2 },
  });
  const verdict = await makeVerify(chromium, { proc: unsandboxed, timeoutMs: 1234 })(server);
  assert.equal(verdict.ok, false);
  assert.ok(events.includes("connect ws://local 1234"), events.join(","));
  const sandboxed = fakeProc({
    10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
    12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2, filters: 3 },
  });
  assert.equal((await makeVerify(chromium, { proc: sandboxed })(server)).ok, true);
});

test("verifyServer on an old kernel reads chrome://sandbox, once its table is there", async () => {
  const events = [];
  const proc = {
    ...fakeProc({
      10: { ppid: 1, cmdline: ["chrome"], userns: 1 },
      12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2 },
    }),
    status: () => "Name:\tchrome\nSeccomp:\t2\n",
  };
  const server = { wsEndpoint: () => "ws://x", process: () => ({ pid: 10 }) };
  const page = fakePage("Layer 1 Sandbox\tNamespace\nSeccomp-BPF sandbox\tYes\nSeccomp-BPF sandbox supports TSYNC\tYes", events);
  const client = { newPage: async () => page, close: async () => events.push("close") };
  const verdict = await verifyServer(server, { connect: async () => client, proc, timeoutMs: 5000 });
  assert.equal(verdict.ok, true);
  assert.equal(verdict.evidence.method, "chrome://sandbox");
  assert.deepEqual(events, ["setContent 5000", "goto chrome://sandbox 5000", "waitForFunction", "close"]);
});

test("the chrome://sandbox match is anchored to its own row", () => {
  const proc = {
    ...fakeProc({
      10: { ppid: 1, cmdline: ["chrome"], userns: 1 },
      12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2 },
    }),
    status: () => "Name:\tchrome\n",
  };
  for (const page of [
    "Seccomp-BPF sandbox\tNo\nSeccomp-BPF sandbox supports TSYNC\tYes",
    "Seccomp-BPF sandbox\nYes",
    "Seccomp-BPF sandbox\tNo\nseccomp-bpf sandbox yes? see docs",
  ]) {
    assert.equal(verifySandbox({ browserPid: "10", proc, sandboxPage: page }).ok, false, JSON.stringify(page));
  }
  assert.equal(verifySandbox({ browserPid: "10", proc, sandboxPage: "Seccomp-BPF sandbox\tYes" }).ok, true);
});

test("an EACCES reading /proc propagates; only exited processes are skipped", () => {
  const proc = {
    ...fakeProc({
      10: { ppid: 1, cmdline: ["chrome"], userns: 1, filters: 2 },
      12: { ppid: 10, cmdline: ["chrome --type=renderer"], userns: 2, filters: 3 },
    }),
  };
  const denied = {
    ...proc,
    status: (pid) => {
      if (pid === "12") {
        const error = new Error("EACCES");
        error.code = "EACCES";
        throw error;
      }
      return proc.status(pid);
    },
  };
  assert.throws(() => verifySandbox({ browserPid: "10", proc: denied }), /EACCES/);
});

test("an attempt that never finishes is bounded, then falls back", async () => {
  const h = harness();
  h.verify = () => new Promise(() => {});
  const { state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h, attemptTimeoutMs: 20 });
  assert.equal(state.sandbox, "off");
  assert.match(state.detail, /did not finish within 0\.02s/);
  assert.deepEqual(h.launches, [true, true, false]);
});

test("a failed first attempt is logged even when the retry succeeds", async () => {
  let attempts = 0;
  const h = harness();
  h.verify = async () => {
    attempts += 1;
    return attempts === 1 ? { ok: false, detail: "a transient failure" } : { ok: true, evidence: { renderers: 1 } };
  };
  const { state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h });
  assert.equal(state.sandbox, "on");
  assert.equal(h.warnings.length, 1);
  assert.match(h.warnings[0], /first sandboxed attempt failed.*a transient failure.*retrying/);
});

test("a no-seccomp hint does not hide a working namespace", () => {
  const failure = classifyFailure({ ...NAMESPACE_OK, profile: "no-seccomp", chromiumError: CHROMIUM_FATAL });
  assert.equal(failure.cause, "chromium_refused");
});

// ---- the sandboxed phase's one deadline (hardening4) ----

const BUDGET = { phaseBudgetMs: 120, minAttemptMs: 40, fallbackLaunchMs: 80 };
const never = () => new Promise(() => {});

test("auto: a sandboxed launch that never resolves falls back within the phase budget, and says why", async () => {
  const h = harness();
  const launches = [];
  h.launch = (sandboxed) => {
    launches.push(sandboxed);
    return sandboxed ? never() : Promise.resolve(fakeServer("unsandboxed"));
  };
  const started = Date.now();
  const { state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h, ...BUDGET });
  const elapsed = Date.now() - started;
  assert.equal(state.sandbox, "off");
  assert.equal(state.cause, "launch_timeout");
  assert.match(state.detail, /did not finish within the 0\.12s sandbox phase budget/);
  assert.deepEqual(launches, [true, false], "no second attempt once the budget is spent");
  assert.ok(elapsed < BUDGET.phaseBudgetMs + 60, `took ${elapsed} ms`);
});

test("on: a sandboxed launch that never resolves is refused within the phase budget", async () => {
  const h = harness();
  h.launch = (sandboxed) => (sandboxed ? never() : Promise.resolve(fakeServer("unsandboxed")));
  const started = Date.now();
  await assert.rejects(
    launchWithSandboxPolicy({ setting: "on", profile: "auto-browser", ...h, ...BUDGET }),
    (error) => error instanceof SandboxRefusedError && error.state.cause === "launch_timeout",
  );
  assert.ok(Date.now() - started < BUDGET.phaseBudgetMs + 60);
});

test("a slow first attempt that eats the budget gets no second attempt", async () => {
  const h = harness({ verified: false });
  const launches = [];
  h.launch = async (sandboxed) => {
    launches.push(sandboxed);
    if (sandboxed) await new Promise((resolve) => setTimeout(resolve, 90)); // 90 of 120 ms: 30 left, under 40
    return fakeServer(sandboxed ? "sandboxed" : "unsandboxed");
  };
  const { state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h, ...BUDGET });
  assert.equal(state.sandbox, "off");
  assert.deepEqual(launches, [true, false]);
  assert.match(h.warnings[0], /first sandboxed attempt failed.*no second attempt/);
});

test("the unsandboxed fallback launch is bounded too", async () => {
  const h = harness({ sandboxedLaunch: "throws" });
  h.launch = (sandboxed) => (sandboxed ? Promise.reject(CHROMIUM_FATAL) : never());
  const started = Date.now();
  await assert.rejects(
    launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h, ...BUDGET }),
    /the unsandboxed launch did not finish within 0\.08s/,
  );
  assert.ok(Date.now() - started < BUDGET.phaseBudgetMs + BUDGET.fallbackLaunchMs + 60);
});

test("a launch that resolves after its deadline is closed, not leaked", async () => {
  const h = harness();
  let late;
  const closed = [];
  h.launch = (sandboxed) => {
    if (!sandboxed) return Promise.resolve(fakeServer("unsandboxed"));
    return new Promise((resolve) => {
      late = () => resolve({ ...fakeServer("late"), close: async () => closed.push("late") });
    });
  };
  await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h, ...BUDGET });
  late();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(closed, ["late"]);
});

test("the defaults: a 30 s phase, a 10 s fallback launch, under the 45 s isolated wait", () => {
  assert.equal(SANDBOX_PHASE_BUDGET_MS, 30000);
  assert.equal(FALLBACK_LAUNCH_TIMEOUT_MS, 10000);
  assert.ok(SANDBOX_PHASE_BUDGET_MS + FALLBACK_LAUNCH_TIMEOUT_MS < 45000);
});

test("a verification that hangs ends with the phase, not after its own longer bound", async () => {
  const h = harness();
  h.verify = never;
  const started = Date.now();
  const { state } = await launchWithSandboxPolicy({
    setting: "auto",
    profile: "auto-browser",
    ...h,
    ...BUDGET,
    attemptTimeoutMs: 5000,
  });
  assert.equal(state.sandbox, "off");
  assert.ok(Date.now() - started < BUDGET.phaseBudgetMs + 60, `took ${Date.now() - started} ms`);
});

const SECRET_PATH = "0123456789abcdef0123456789abcdef";

test("redactEndpoints keeps a websocket endpoint's host and port and drops its path", () => {
  const state = JSON.stringify({ sandbox: "on", endpoint: `ws://browser-node:9223/${SECRET_PATH}` });
  const out = redactEndpoints(`state: ${state}; retry ws://127.0.0.1:9223/${SECRET_PATH}?x=1 now`);
  assert.ok(!out.includes(SECRET_PATH), out);
  assert.ok(out.includes("ws://browser-node:9223/<redacted>"), out);
  assert.ok(out.includes("ws://127.0.0.1:9223/<redacted>"), out);
  const failure = `browserType.connect: WebSocket error: wss://h.example:443/${SECRET_PATH} 502`;
  assert.equal(redactEndpoints(failure), "browserType.connect: WebSocket error: wss://h.example:443/<redacted> 502");
  assert.equal(redactEndpoints("no endpoint here"), "no endpoint here");
});

test("sandbox-report prints the state file and its errors without the Playwright connect path", () => {
  const dir = mkdtempSync(join(tmpdir(), "sandbox-report-"));
  try {
    const endpointFile = join(dir, "browser-ws-endpoint.txt");
    writeFileSync(endpointFile, `ws://127.0.0.1:1/${SECRET_PATH}\n`);
    writeFileSync(
      join(dir, "browser-sandbox.json"),
      JSON.stringify({ sandbox: "on", setting: "on", endpoint: `ws://browser-node:9223/${SECRET_PATH}` }),
    );
    const result = spawnSync(process.execPath, [fileURLToPath(new URL("./sandbox-report.mjs", import.meta.url)), "t"], {
      env: { PATH: process.env.PATH, BROWSER_WS_ENDPOINT_FILE: endpointFile },
      encoding: "utf-8",
      timeout: 60000,
    });
    const output = `${result.stdout}${result.stderr}`;
    assert.equal(result.status, 0, output);
    assert.ok(!output.includes(SECRET_PATH), output);
    assert.match(output, /state file: .*"sandbox":"on"/);
    assert.ok(output.includes("ws://browser-node:9223/<redacted>"), output);
    assert.match(output, /live check failed/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
