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


class _ProvisionMixin:
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


class IsolatedContainerSandboxTests(_ProvisionMixin, unittest.IsolatedAsyncioTestCase):
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
        runtime = await self._provision(sandbox_state=ON, BROWSER_SANDBOX="on")
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
        # As written, plus the endpoint of the run it describes (the fake writes it, as server.mjs does).
        expected = {**state, "endpoint": "ws://browser-session-sbx:9223/playwright"}
        self.assertEqual(runtime.sandbox, expected)
        self.assertEqual(self.provisioner.sandbox_states(), {"sbx": expected})
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
            # Nothing there at all: not reported yet (round 4, S3#3), not absent.
            self.assertEqual(browser_sandbox_status(settings, provisioner)["browser_node"]["sandbox"], "unknown")
            endpoint.write_text("ws://browser-node:9223/x", encoding="utf-8")
            # An endpoint but no connection yet: unknown, never the file read here (the snapshot is taken at connect).
            (Path(tempdir) / BROWSER_SANDBOX_STATE_FILENAME).write_text('{"sandbox": "on"}', encoding="utf-8")
            status = browser_sandbox_status(settings, provisioner)
            self.assertEqual(status["browser_node"]["sandbox"], "unknown")
            status = browser_sandbox_status(settings, provisioner, shared={"sandbox": "on"})
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


ON = {
    "setting": "auto",
    "sandbox": "on",
    "cause": None,
    "verified": {
        "renderers": 1,
        "renderer_userns_differs": True,
        "browser_seccomp_filters": 2,
        "renderer_seccomp_filters": [3],
    },
}


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
            server.index("await writeState(sandboxStateFile, { ...result.state"),
            server.index("await rename(tmpFile, endpointFile)"),
        )

    def test_the_image_ships_the_policy_and_the_entrypoint_clears_a_stale_state(self) -> None:
        dockerfile = (BROWSER_NODE / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^COPY server\.mjs sandbox\.mjs sandbox-report\.mjs /opt/browser-node/$")
        entrypoint = (BROWSER_NODE / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('rm -rf -- "$WS_ENDPOINT_FILE" "$SANDBOX_STATE_FILE"', entrypoint)
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


# ---- round 2: a self-attested state, read safely; `on` enforced by the controller; refusals visible ----

import os as _os  # noqa: E402
import shutil as _shutil  # noqa: E402
import sys as _sys  # noqa: E402

from app.browser_sandbox import STATE_MAX_BYTES  # noqa: E402


class HardenedStateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / BROWSER_SANDBOX_STATE_FILENAME

    def test_forged_keys_are_dropped_and_strings_capped(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "sandbox": "on",
                    "cause": None,
                    "detail": "d" * 5000,
                    "anything": {"x": 1},
                    "verified": {"renderers": 2, "renderer_seccomp_filters": [3] * 500, "token": "x"},
                }
            ),
            encoding="utf-8",
        )
        state = read_sandbox_state(self.path)
        self.assertEqual(set(state), {"sandbox", "cause", "detail", "verified"})
        self.assertLessEqual(len(state["detail"]), 1000)
        # A 500-entry filter list is dropped whole (S5#8), not cut to 64.
        self.assertEqual(set(state["verified"]), {"renderers"})

    def test_wrong_types_and_unknown_states_are_unreadable(self) -> None:
        self.path.write_text('{"sandbox": 5}', encoding="utf-8")
        self.assertEqual(read_sandbox_state(self.path)["sandbox"], "unreadable")
        self.path.write_text('{"sandbox": "maybe"}', encoding="utf-8")
        self.assertEqual(read_sandbox_state(self.path)["sandbox"], "unreadable")
        self.path.write_text('["on"]', encoding="utf-8")
        self.assertEqual(read_sandbox_state(self.path)["sandbox"], "unreadable")

    def test_a_50_mb_file_is_not_read(self) -> None:
        with self.path.open("wb") as handle:
            handle.truncate(50 * 1024 * 1024)
        state = read_sandbox_state(self.path)
        self.assertEqual(state["sandbox"], "unreadable")
        self.assertIn(str(STATE_MAX_BYTES), state["detail"])

    @unittest.skipUnless(_sys.platform.startswith("linux"), "O_NOFOLLOW and symlinks: Linux")
    def test_a_symlink_is_not_followed(self) -> None:
        secret = Path(self.tmp.name) / "auth-profile.json"
        secret.write_text('{"sandbox": "on", "detail": "cookies"}', encoding="utf-8")
        _os.symlink(secret, self.path)
        state = read_sandbox_state(self.path)
        self.assertEqual(state["sandbox"], "unreadable")
        self.assertNotIn("cookies", json.dumps(state))


class SharedNodeStatusTests(unittest.TestCase):
    """The shared node's state comes from the connect-time snapshot (tests/test_shared_sandbox.py), except a refusal:
    a browser-node that refused to start sandboxed writes its state and no endpoint."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.endpoint = Path(self.tmp.name) / "browser-ws-endpoint.txt"
        self.state = Path(self.tmp.name) / BROWSER_SANDBOX_STATE_FILENAME
        self.settings = SimpleNamespace(
            browser_sandbox="auto",
            browser_ws_endpoint_file=str(self.endpoint),
            session_isolation_mode="shared_browser_node",
            isolated_browser_seccomp_profile=str(SECCOMP_PROFILE_PATH),
        )

    def test_a_refusal_without_an_endpoint_file_is_reported_and_fails(self) -> None:
        self.state.write_text(
            '{"setting": "on", "sandbox": "refused", "cause": "userns_refused", "detail": "AppArmor"}', encoding="utf-8"
        )
        status = browser_sandbox_status(self.settings)
        self.assertEqual(status["browser_node"]["sandbox"], "refused")
        check = browser_sandbox_check(status)
        self.assertEqual(check.status, "fail")
        self.assertIn("userns_refused", check.message)

    def test_under_on_a_shared_node_not_on_fails(self) -> None:
        self.settings.browser_sandbox = "on"
        self.endpoint.write_text("ws://browser-node:9223/run-1", encoding="utf-8")
        for snapshot in ({"sandbox": "off", "cause": "setting_off"}, {"sandbox": "unknown"}, None):
            with self.subTest(snapshot=snapshot):
                status = browser_sandbox_status(self.settings, shared=snapshot)
                self.assertEqual(browser_sandbox_check(status).status, "fail")


class OnEnforcedForIsolatedTests(_ProvisionMixin, unittest.IsolatedAsyncioTestCase):
    async def test_on_refuses_a_container_whose_state_is_not_on(self) -> None:
        for state in (
            {"sandbox": "off", "cause": "userns_refused", "detail": "AppArmor"},
            None,
            {"sandbox": "unreadable", "detail": "not valid JSON"},
            {"sandbox": "on"},  # no verification evidence
            # a verified on that names another run's endpoint
            {
                "sandbox": "on",
                "verified": {
                    "renderers": 1,
                    "renderer_userns_differs": True,
                    "browser_seccomp_filters": 2,
                    "renderer_seccomp_filters": [3],
                },
                "endpoint": "ws://another-run:9223/x",
            },
            # a verified on that names no endpoint (S3#15: required for isolated states too)
            {
                "sandbox": "on",
                "verified": {
                    "renderers": 1,
                    "renderer_userns_differs": True,
                    "browser_seccomp_filters": 2,
                    "renderer_seccomp_filters": [3],
                },
                "endpoint": None,
            },
        ):
            with self.subTest(state=state):
                with self.assertRaisesRegex(
                    RuntimeError, r"BROWSER_SANDBOX=on, but .* reports .*; the session was refused"
                ):
                    await self._provision(sandbox_state=state, BROWSER_SANDBOX="on")
                self.assertEqual(self.provisioner.sandbox_states(), {})
                container = next(iter(self.client.containers.browser_containers.values()))
                self.assertTrue(container.removed)

    async def test_cb1_7_the_refusal_claims_no_removal_the_rollback_may_not_make(self) -> None:
        # "Its container was removed." was written before the rollback ran: wrong when its removal failed, and under
        # ISOLATED_BROWSER_KEEP_CONTAINERS, which only stops the container.
        off = {"sandbox": "off", "cause": "userns_refused"}
        for keep in ("false", "true"):
            with self.subTest(keep=keep):
                with self.assertRaisesRegex(RuntimeError, "the session was refused") as raised:
                    await self._provision(
                        sandbox_state=off, BROWSER_SANDBOX="on", ISOLATED_BROWSER_KEEP_CONTAINERS=keep
                    )
                self.assertNotIn("removed", str(raised.exception))
                container = next(iter(self.client.containers.browser_containers.values()))
                self.assertEqual(container.removed, keep == "false")
                self.assertTrue(container.stopped)

    async def test_auto_keeps_a_fallen_back_container(self) -> None:
        runtime = await self._provision(sandbox_state={"sandbox": "off", "cause": "userns_refused"})
        self.assertEqual(runtime.sandbox["sandbox"], "off")

    async def test_an_exit_before_the_endpoint_carries_the_state(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_root = Path(tempdir.name) / "data"
        data_root.mkdir(parents=True)
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        client = FakeDockerClient(controller)
        client.containers.sandbox_state = {
            "sandbox": "refused",
            "cause": "userns_refused",
            "detail": "AppArmor said no",
        }
        client.containers.exit_before_endpoint = True
        provisioner = DockerBrowserNodeProvisioner(_settings(data_root, BROWSER_SANDBOX="on"), client=client)
        provisioner._controller_container_id = controller.id
        with self.assertRaisesRegex(
            RuntimeError, r"exited before.*refused \(userns_refused\): AppArmor said no"
        ) as raised:
            await provisioner.provision("gone")
        # S#12: the state ends its sentence, and the container's logs start on a line of their own (N13).
        self.assertIn("AppArmor said no.\n", str(raised.exception))


class SeccompSupportTests(_ProvisionMixin, unittest.IsolatedAsyncioTestCase):
    async def _no_seccomp(self, **overrides):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_root = Path(tempdir.name) / "data"
        data_root.mkdir(parents=True)
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        client = FakeDockerClient(controller)
        client.security_options = ["name=cgroupns"]
        client.containers.sandbox_state = {"sandbox": "off", "cause": "seccomp_unsupported"}
        provisioner = DockerBrowserNodeProvisioner(_settings(data_root, **overrides), client=client)
        provisioner._controller_container_id = controller.id
        self.client = client
        return provisioner

    async def test_auto_sends_no_profile_to_a_daemon_without_seccomp(self) -> None:
        provisioner = await self._no_seccomp()
        runtime = await provisioner.provision("noseccomp")
        kwargs = self.client.containers.browser_containers[runtime.container_name].run_kwargs
        self.assertEqual(kwargs["security_opt"], [NNP])
        self.assertEqual(kwargs["environment"]["BROWSER_SECCOMP_PROFILE"], "no-seccomp")

    async def test_on_refuses_before_anything_exists(self) -> None:
        provisioner = await self._no_seccomp(BROWSER_SANDBOX="on")
        with self.assertRaisesRegex(RuntimeError, "BROWSER_SANDBOX=on.*no seccomp"):
            await provisioner.provision("noseccomp")
        self.assertEqual(self.client.containers.browser_containers, {})

    async def test_a_custom_profile_path_is_named_custom(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        custom = Path(tempdir.name) / "mine.json"
        _shutil.copy(SECCOMP_PROFILE_PATH, custom)
        runtime = await self._provision(ISOLATED_BROWSER_SECCOMP_PROFILE=str(custom))
        self.assertEqual(self._kwargs(runtime)["environment"]["BROWSER_SECCOMP_PROFILE"], "custom")


class SettingParsingTests(unittest.TestCase):
    def test_browser_sandbox_is_trimmed_and_lowercased_like_browser_node(self) -> None:
        for raw, expected in ((" On ", "on"), ("OFF", "off"), ("auto\n", "auto"), ("", "auto")):
            self.assertEqual(Settings(_env_file=None, BROWSER_SANDBOX=raw).browser_sandbox, expected)

    def test_an_empty_profile_path_means_the_shipped_one(self) -> None:
        settings = Settings(_env_file=None, ISOLATED_BROWSER_SECCOMP_PROFILE="  ")
        self.assertEqual(Path(settings.isolated_browser_seccomp_profile), SECCOMP_PROFILE_PATH)


@unittest.skipUnless(COMPOSE.is_file(), "browser-node/ is not shipped in the controller image")
class Round2WiringTests(unittest.TestCase):
    def test_server_mjs_verifies_through_sandbox_mjs(self) -> None:
        server = (BROWSER_NODE / "server.mjs").read_text(encoding="utf-8")
        # Exactly the factory, unwrapped: anything around it (a .then that forces ok) is a different line.
        self.assertRegex(server, r"(?m)^const verify = makeVerify\(chromium\);$")
        # And that verify, unwrapped, is what the policy gets (R2c wrapped it in the call).
        call = server[server.index("await launchWithSandboxPolicy({") :]
        call = call[: call.index("});")]
        self.assertRegex(call, r"(?m)^    verify,$")
        self.assertNotIn("verify:", call)
        self.assertNotIn("async function verify(", server)
        self.assertNotIn("verifySandbox(", server)
        self.assertIn(
            "await writeState(sandboxStateFile, { ...result.state, endpoint: advertised(browserServer) });", server
        )

    def test_browser_node_output_reaches_docker_logs(self) -> None:
        entrypoint = (BROWSER_NODE / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("server.mjs > >(tee /tmp/playwright-server.log) 2>&1 &", entrypoint)
        # PID 1 may exit before tee flushes: a refusal also prints the state directly.
        self.assertIn('its sandbox state: $(cat "$SANDBOX_STATE_FILE" 2>/dev/null || echo none)', entrypoint)
        # Only for a failure of its own (a refusal exits 1), not for the 143 of every docker stop (S3#9).
        self.assertIn("if (( status > 0 && status < 128 )); then", entrypoint)

    def test_ci_has_a_sandbox_on_leg_that_asserts_it_engaged(self) -> None:
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = ci[ci.index("  compose-smoke:") :]
        self.assertIn("sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0", job)
        self.assertIn("BROWSER_SANDBOX=on", job)
        self.assertIn("scripts/assert_browser_sandbox.py data/browser-profile/browser-sandbox.json --require-proc", job)
        # The negative leg: on without the profile must refuse, leaving no endpoint.
        self.assertIn("BROWSER_SANDBOX=on BROWSER_SECCOMP_PROFILE=builtin docker compose up", job)
        self.assertIn('if state and state.get("sandbox") == "refused":', job)
        self.assertIn('sys.exit("a refused browser-node left an endpoint file")', job)
        # The controller under on in both legs: a session on the verified node, and a 503 on the refused one, which
        # publishes no endpoint to connect to (S3#5; an unreachable browser-node answers 503 since R-2).
        self.assertIn("BROWSER_SANDBOX=on docker compose up -d --force-recreate browser-node controller", job)
        # S4#1/D9: a leg only the gate can refuse (an unverified browser-node with an endpoint), and its WARNING.
        self.assertIn(
            "BROWSER_SANDBOX=auto BROWSER_SECCOMP_PROFILE=builtin docker compose up -d --force-recreate --no-deps "
            "browser-node",
            job,
        )
        self.assertIn('--log-must-say "BROWSER_SANDBOX=on, but the shared browser-node reports off"', job)
        # CI2 and S5#12: the leg fails without an endpoint, and checks the state is off before the gate.
        self.assertIn('|| { echo "browser-node published no endpoint under auto" >&2; exit 1; }', job)
        self.assertIn('sys.exit(s.get("sandbox") != "off")', job)
        # D10: an isolated session under on, its container's state verified.
        self.assertIn("check_sandbox_gate.py --expect 200 --isolated", job)
        self.assertLess(
            job.index('check_sandbox_gate.py --expect 200 --api-port "$api_port"'),
            job.index('check_sandbox_gate.py --expect 503 --api-port "$api_port"'),
        )
        self.assertLess(
            job.index('check_sandbox_gate.py --expect 503 --api-port "$api_port"'),
            job.index('check_sandbox_gate.py --expect 409 --api-port "$api_port"'),
        )
        script = (REPO / "scripts" / "assert_browser_sandbox.py").read_text(encoding="utf-8")
        self.assertIn('state.get("sandbox") != "on"', script)
        self.assertIn("renderers", script)


@unittest.skipUnless((REPO / "scripts" / "assert_browser_sandbox.py").is_file(), "scripts/ is not in the image")
class AssertSandboxScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "assert_browser_sandbox", REPO / "scripts" / "assert_browser_sandbox.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_setting_on_and_proc_evidence_are_required(self) -> None:
        proc = {
            "renderers": 3,
            "renderer_userns_differs": True,
            "browser_seccomp_filters": 2,
            "renderer_seccomp_filters": [3],
        }
        # auto engaging on a lifted runner would hide BROWSER_SANDBOX not reaching browser-node.
        self.assertTrue(
            any("setting" in p for p in self.module.problems({"setting": "auto", "sandbox": "on", "verified": proc}))
        )
        via_page = {"renderers": 1, "renderer_userns_differs": True, "method": "chrome://sandbox"}
        state = {"setting": "on", "sandbox": "on", "verified": via_page}
        self.assertEqual(self.module.problems(state), [])
        self.assertNotEqual(self.module.problems(state, require_proc=True), [])
        self.assertEqual(
            self.module.problems({"setting": "on", "sandbox": "on", "verified": proc}, require_proc=True), []
        )

    def test_an_engaged_verified_sandbox_passes(self) -> None:
        state = {
            "setting": "on",
            "sandbox": "on",
            "verified": {
                "renderers": 3,
                "renderer_userns_differs": True,
                "browser_seccomp_filters": 2,
                "renderer_seccomp_filters": [3, 3, 3],
            },
        }
        self.assertEqual(self.module.problems(state), [])

    def test_anything_less_fails(self) -> None:
        for state in (
            {"sandbox": "off", "cause": "userns_refused"},
            {"sandbox": "on", "verified": {}},
            {"sandbox": "on", "verified": {"renderers": 0, "renderer_userns_differs": True}},
            {
                "sandbox": "on",
                "verified": {
                    "renderers": 1,
                    "renderer_userns_differs": False,
                    "browser_seccomp_filters": 2,
                    "renderer_seccomp_filters": [3],
                },
            },
            {
                "sandbox": "on",
                "verified": {
                    "renderers": 1,
                    "renderer_userns_differs": True,
                    "browser_seccomp_filters": 2,
                    "renderer_seccomp_filters": [2],
                },
            },
        ):
            with self.subTest(state=state):
                self.assertNotEqual(self.module.problems(state), [])


if __name__ == "__main__":
    unittest.main()
