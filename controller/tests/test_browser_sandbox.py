"""Chromium's sandbox in the browser containers: BROWSER_SANDBOX=auto|on|off.

browser-node/server.mjs launches Chromium sandboxed (sandbox.mjs; its own tests are browser-node/sandbox.test.mjs,
run with `node --test`). That needs the seccomp profile in app/browser/seccomp/: moby's default plus one entry
allowing chroot, clone and unshare. These tests pin that profile to its upstream source, check that compose and the
controller apply it only when the sandbox is wanted, and that readiness reports the state each browser wrote, a
fallback included.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.browser_sandbox import (
    BROWSER_SANDBOX_STATE_FILENAME,
    SECCOMP_PROFILE_PATH,
    browser_sandbox_check,
    browser_sandbox_status,
    read_sandbox_state,
)
from app.config import Settings
from app.readiness import run_readiness_checks
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

SECCOMP_DIR = Path(__file__).resolve().parents[1] / "app" / "browser" / "seccomp"
REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
BROWSER_NODE = REPO / "browser-node"
MOBY_SHA256 = "785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334"
NNP = "no-new-privileges:true"


def _auto_browser_entries(profile: dict) -> list[dict]:
    return [entry for entry in profile["syscalls"] if str(entry.get("comment", "")).startswith("auto-browser:")]


class SeccompProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.moby_bytes = (SECCOMP_DIR / "moby-default.json").read_bytes()
        self.moby = json.loads(self.moby_bytes)
        self.profile = json.loads((SECCOMP_DIR / "chromium-sandbox.json").read_text(encoding="utf-8"))

    def test_the_pinned_moby_default_is_byte_for_byte_upstream(self) -> None:
        # docker-v29.9.2 vendor/github.com/moby/profiles/seccomp/default.json (module v0.2.4); see README.md.
        self.assertEqual(hashlib.sha256(self.moby_bytes).hexdigest(), MOBY_SHA256)

    def test_exactly_one_entry_is_ours_and_it_allows_only_chroot_clone_and_unshare(self) -> None:
        ours = _auto_browser_entries(self.profile)
        self.assertEqual(len(ours), 1)
        self.assertIn("Chromium's namespace sandbox", ours[0]["comment"])
        self.assertEqual(sorted(ours[0]["names"]), ["chroot", "clone", "unshare"])
        self.assertEqual(ours[0]["action"], "SCMP_ACT_ALLOW")
        self.assertEqual(set(ours[0]), {"names", "action", "comment"}, "no args, includes or excludes")
        self.assertEqual(_auto_browser_entries(self.moby), [])

    def test_nothing_else_differs_from_moby(self) -> None:
        stripped = dict(self.profile)
        stripped["syscalls"] = [
            entry for entry in self.profile["syscalls"] if entry not in _auto_browser_entries(self.profile)
        ]
        self.assertEqual(stripped, self.moby)

    def test_the_readme_records_the_provenance(self) -> None:
        readme = (SECCOMP_DIR / "README.md").read_text(encoding="utf-8")
        for fact in ("docker-v29.9.2", "vendor/github.com/moby/profiles/seccomp/default.json", "v0.2.4", MOBY_SHA256):
            self.assertIn(fact, readme)

    def test_the_controller_loads_the_profile_it_ships(self) -> None:
        self.assertEqual(SECCOMP_PROFILE_PATH.resolve(), (SECCOMP_DIR / "chromium-sandbox.json").resolve())


def _settings(data_root: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        ARTIFACT_ROOT=str(data_root / "artifacts"),
        ISOLATED_HOST_DATA_ROOT=str(data_root),
        **overrides,
    )


class IsolatedContainerSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def _provision(self, sandbox_state: dict | None = None, **overrides):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_root = Path(tempdir.name) / "data"
        data_root.mkdir(parents=True)
        settings = _settings(data_root, **overrides)
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        client = FakeDockerClient(controller)
        client.containers.sandbox_state = sandbox_state
        provisioner = DockerBrowserNodeProvisioner(settings, client=client)
        provisioner._controller_container_id = controller.id
        self.client = client
        self.provisioner = provisioner
        return await provisioner.provision("sbx")

    def _kwargs(self, runtime) -> dict:
        return self.client.containers.browser_containers[runtime.container_name].run_kwargs

    async def test_auto_carries_the_profile_json_itself_not_a_path(self) -> None:
        runtime = await self._provision()
        security_opt = self._kwargs(runtime)["security_opt"]
        self.assertEqual(security_opt[0], NNP)
        self.assertEqual(len(security_opt), 2)
        self.assertTrue(security_opt[1].startswith("seccomp={"), security_opt[1][:40])
        sent = json.loads(security_opt[1].removeprefix("seccomp="))
        self.assertEqual(sent, json.loads(SECCOMP_PROFILE_PATH.read_text(encoding="utf-8")))
        environment = self._kwargs(runtime)["environment"]
        self.assertEqual(environment["BROWSER_SANDBOX"], "auto")
        self.assertEqual(environment["BROWSER_SECCOMP_PROFILE"], "auto-browser")

    async def test_on_carries_the_profile_too(self) -> None:
        runtime = await self._provision(BROWSER_SANDBOX="on")
        self.assertEqual(len(self._kwargs(runtime)["security_opt"]), 2)
        self.assertEqual(self._kwargs(runtime)["environment"]["BROWSER_SANDBOX"], "on")

    async def test_off_applies_no_custom_profile(self) -> None:
        runtime = await self._provision(BROWSER_SANDBOX="off")
        self.assertEqual(self._kwargs(runtime)["security_opt"], [NNP])
        environment = self._kwargs(runtime)["environment"]
        self.assertEqual(environment["BROWSER_SANDBOX"], "off")
        self.assertEqual(environment["BROWSER_SECCOMP_PROFILE"], "builtin")

    async def test_on_without_the_profile_file_refuses_before_anything_exists(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"BROWSER_SANDBOX=on.*missing\.json"):
            await self._provision(BROWSER_SANDBOX="on", ISOLATED_BROWSER_SECCOMP_PROFILE="/nonexistent/missing.json")
        self.assertEqual(self.client.containers.browser_containers, {})
        self.assertEqual([event for event in self.client.events if event[0] == "network.create"], [])

    async def test_auto_without_the_profile_file_starts_under_dockers_default_and_says_so(self) -> None:
        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            runtime = await self._provision(ISOLATED_BROWSER_SECCOMP_PROFILE="/nonexistent/missing.json")
        self.assertEqual(self._kwargs(runtime)["security_opt"], [NNP])
        self.assertEqual(self._kwargs(runtime)["environment"]["BROWSER_SECCOMP_PROFILE"], "builtin")
        self.assertRegex("\n".join(logs.output), r"missing\.json.*sandbox")

    async def test_the_state_the_container_wrote_is_tracked_until_release(self) -> None:
        state = {"setting": "auto", "sandbox": "off", "cause": "userns_refused", "detail": "the kernel or AppArmor"}
        runtime = await self._provision(sandbox_state=state)
        self.assertEqual(runtime.sandbox, state)
        self.assertEqual(self.provisioner.sandbox_states(), {"sbx": state})
        await self.provisioner.release(runtime)
        self.assertEqual(self.provisioner.sandbox_states(), {})


class SandboxStateFileTests(unittest.TestCase):
    def test_reads_a_state_and_tolerates_absent_or_broken_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / BROWSER_SANDBOX_STATE_FILENAME
            self.assertIsNone(read_sandbox_state(path))
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(read_sandbox_state(path), {"sandbox": "unreadable", "detail": f"{path} is not valid JSON"})
            path.write_text('{"sandbox": "on"}', encoding="utf-8")
            self.assertEqual(read_sandbox_state(path), {"sandbox": "on"})

    def test_status_reads_the_shared_node_beside_its_endpoint_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            endpoint = Path(tempdir) / "browser-ws-endpoint.txt"
            settings = SimpleNamespace(
                browser_sandbox="auto",
                browser_ws_endpoint_file=str(endpoint),
                session_isolation_mode="shared_browser_node",
                isolated_browser_seccomp_profile=str(SECCOMP_PROFILE_PATH),
            )
            provisioner = SimpleNamespace(sandbox_states=lambda: {"s1": {"sandbox": "on"}})
            self.assertIsNone(browser_sandbox_status(settings, provisioner)["browser_node"])
            endpoint.write_text("ws://browser-node:9223/x", encoding="utf-8")
            status = browser_sandbox_status(settings, provisioner)
            self.assertEqual(status["browser_node"], {"sandbox": "unknown"})
            (Path(tempdir) / BROWSER_SANDBOX_STATE_FILENAME).write_text('{"sandbox": "on"}', encoding="utf-8")
            status = browser_sandbox_status(settings, provisioner)
            self.assertEqual(status["browser_node"], {"sandbox": "on"})
            self.assertEqual(status["isolated"], {"s1": {"sandbox": "on"}})
            self.assertTrue(status["profile_readable"])


def _status(browser_node=None, isolated=None, *, setting="auto", mode="shared_browser_node", readable=True) -> dict:
    return {
        "setting": setting,
        "isolation_mode": mode,
        "profile_path": "/app/app/browser/seccomp/chromium-sandbox.json",
        "profile_readable": readable,
        "browser_node": browser_node,
        "isolated": isolated or {},
    }


ON = {"setting": "auto", "sandbox": "on", "cause": None, "verified": {"renderers": 1}}


class ReadinessSandboxTests(unittest.TestCase):
    def test_every_browser_sandboxed_passes(self) -> None:
        check = browser_sandbox_check(_status(ON, {"s1": ON}))
        self.assertEqual(check.name, "browser_sandbox")
        self.assertEqual(check.status, "pass")
        self.assertIn("2 browser", check.message)

    def test_a_fallback_is_never_silent(self) -> None:
        fell_back = {
            "setting": "auto",
            "sandbox": "off",
            "cause": "userns_refused",
            "detail": "the kernel or AppArmor refused to create a user namespace",
        }
        check = browser_sandbox_check(_status(ON, {"s1": fell_back}))
        self.assertEqual(check.status, "warn")
        self.assertIn("session s1", check.message)
        self.assertIn("userns_refused", check.message)
        self.assertIn("the kernel or AppArmor refused", check.message)
        self.assertEqual(check.details["isolated"]["s1"], fell_back)

    def test_the_profile_not_applied_reads_differently(self) -> None:
        not_applied = {"sandbox": "off", "cause": "seccomp_profile_not_applied", "detail": "is not applied"}
        check = browser_sandbox_check(_status(not_applied))
        self.assertEqual(check.status, "warn")
        self.assertIn("browser-node: sandbox off (seccomp_profile_not_applied)", check.message)
        self.assertNotIn("AppArmor", check.message)

    def test_off_by_setting_warns(self) -> None:
        off = {"setting": "off", "sandbox": "off", "cause": "setting_off", "detail": "BROWSER_SANDBOX=off"}
        check = browser_sandbox_check(_status(off, setting="off"))
        self.assertEqual(check.status, "warn")
        self.assertIn("setting_off", check.message)

    def test_unknown_states_warn(self) -> None:
        check = browser_sandbox_check(_status({"sandbox": "unknown"}, {"s1": None}))
        self.assertEqual(check.status, "warn")
        self.assertIn("browser-node: sandbox state unknown", check.message)
        self.assertIn("session s1: sandbox state unknown", check.message)

    def test_on_with_isolation_and_no_profile_fails(self) -> None:
        check = browser_sandbox_check(_status(ON, setting="on", mode="docker_ephemeral", readable=False))
        self.assertEqual(check.status, "fail")
        self.assertIn("BROWSER_SANDBOX=on", check.message)

    def test_nothing_to_report_yet_passes_quietly(self) -> None:
        check = browser_sandbox_check(_status(None))
        self.assertEqual(check.status, "pass")
        self.assertIn("No browser", check.message)

    def test_readiness_includes_it_when_given(self) -> None:
        report = run_readiness_checks(Settings(_env_file=None), browser_sandbox=_status(ON))
        self.assertIn("browser_sandbox", [check.name for check in report.checks])
        report = run_readiness_checks(Settings(_env_file=None))
        self.assertNotIn("browser_sandbox", [check.name for check in report.checks])


class SettingTests(unittest.TestCase):
    def test_defaults_to_auto_and_rejects_other_values(self) -> None:
        self.assertEqual(Settings(_env_file=None).browser_sandbox, "auto")
        self.assertEqual(Settings(_env_file=None, BROWSER_SANDBOX="off").browser_sandbox, "off")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, BROWSER_SANDBOX="yes")


@unittest.skipUnless(COMPOSE.is_file(), "docker-compose.yml and browser-node/ are not shipped in the controller image")
class ComposeAndImageWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_container_hardening import _flow_list, _service_block

        compose = COMPOSE.read_text(encoding="utf-8")
        self.browser_node = _service_block(compose, "browser-node")
        self.controller = _service_block(compose, "controller")
        self.flow_list = _flow_list

    def test_browser_node_gets_the_profile_unless_told_builtin(self) -> None:
        opts = self.flow_list(self.browser_node, "security_opt")
        self.assertEqual(
            opts, [NNP, "seccomp=${BROWSER_SECCOMP_PROFILE:-./controller/app/browser/seccomp/chromium-sandbox.json}"]
        )
        self.assertTrue((REPO / "controller/app/browser/seccomp/chromium-sandbox.json").is_file())

    def test_both_services_read_browser_sandbox(self) -> None:
        self.assertRegex(self.browser_node, r"(?m)^      BROWSER_SANDBOX: \$\{BROWSER_SANDBOX:-auto\}$")
        self.assertRegex(
            self.browser_node, r"(?m)^      BROWSER_SECCOMP_PROFILE: \$\{BROWSER_SECCOMP_PROFILE:-auto-browser\}$"
        )
        self.assertRegex(self.controller, r"(?m)^      BROWSER_SANDBOX: \$\{BROWSER_SANDBOX:-auto\}$")

    def test_server_launches_through_the_policy(self) -> None:
        server = (BROWSER_NODE / "server.mjs").read_text(encoding="utf-8")
        self.assertNotRegex(server, r"chromiumSandbox:\s*false")
        self.assertIn("launchWithSandboxPolicy(", server)
        self.assertIn("chromiumSandbox: sandboxed", server)
        # The state is written before the endpoint file, so a reader that sees the endpoint sees the state.
        self.assertLess(
            server.index("await writeState(sandboxStateFile, result.state)"),
            server.index("await rename(tmpFile, endpointFile)"),
        )

    def test_the_image_ships_the_policy_and_the_entrypoint_clears_a_stale_state(self) -> None:
        dockerfile = (BROWSER_NODE / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^COPY server\.mjs sandbox\.mjs sandbox-report\.mjs /opt/browser-node/$")
        entrypoint = (BROWSER_NODE / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('rm -f "$WS_ENDPOINT_FILE" "$SANDBOX_STATE_FILE"', entrypoint)
        self.assertIn("browser-sandbox.json", entrypoint)

    def test_the_smokes_print_each_containers_measured_state(self) -> None:
        smoke = (REPO / "scripts" / "smoke_isolated_session.sh").read_text(encoding="utf-8")
        self.assertIn('docker exec -u browser "$1" node /opt/browser-node/sandbox-report.mjs "$2"', smoke)
        for call in (
            'sandbox_report "${CONTAINER_NAME}" "session A"',
            'sandbox_report "${B_CONTAINER_NAME}" "session B"',
            'sandbox_report "${BROWSER_NODE_ID}" "browser-node"',
        ):
            self.assertIn(call, smoke)
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = ci[ci.index("  compose-smoke:") :]
        self.assertIn(
            "run: docker compose exec -T -u browser browser-node node /opt/browser-node/sandbox-report.mjs browser-node",
            job,
        )
        self.assertIn("run: node --test sandbox.test.mjs", ci)
        # Printed, not asserted: the report never fails the step.
        report = (BROWSER_NODE / "sandbox-report.mjs").read_text(encoding="utf-8")
        self.assertNotIn("process.exit(1)", report)


if __name__ == "__main__":
    unittest.main()
