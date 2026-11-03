// node --test browser-node/  (CI runs it; locally: inside the browser-node image, see scripts in REPORT)
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  classifyFailure,
  declaredProfile,
  launchWithSandboxPolicy,
  verifyServer,
  parseSetting,
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
  assert.equal(h.warnings.length, 1);
  assert.match(h.warnings[0], /^WARNING: Chromium's sandbox is OFF/);
  assert.match(h.warnings[0], /seccomp_profile_not_applied/);
  assert.match(h.warnings[0], /not applied/);
});

test("auto: a launch whose renderers are not actually sandboxed is closed and relaunched unsandboxed", async () => {
  const h = harness({ verified: false });
  const { server, state } = await launchWithSandboxPolicy({ setting: "auto", profile: "auto-browser", ...h });
  assert.deepEqual(h.launches, [true, true, false]);
  assert.equal(server.name, "unsandboxed");
  assert.equal(state.sandbox, "off");
  assert.equal(state.cause, "not_verified");
  assert.match(state.detail, /no renderer runs in its own user namespace/);
  assert.equal(h.warnings.length, 1);
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
  assert.deepEqual(h.warnings.length, 0);
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
