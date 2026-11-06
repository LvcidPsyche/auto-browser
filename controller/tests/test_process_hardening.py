"""The controller is non-dumpable and every child it starts gets an allowlisted environment (W2-27).

The controller starts children that run as its own uid (the provider CLIs, the Universal Verifier command, autossh,
the regex worker). proc(5): a same-uid process reads /proc/PID/environ of a dumpable target, so an unsandboxed child
could read the controller's whole environment around the allowlisted one it was given. The controller (and the
Codex host bridge) now call prctl(PR_SET_DUMPABLE, 0) at startup, and the children that got the controller's whole
environment get an allowlisted one.

The /proc probes run in a separate Python process, so this test runner stays dumpable; they print only a status,
never what a read returned.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from app.config import Settings
from app.harness.contracts import TaskContract
from app.harness.verifier.uv import UniversalVerifierAdapter

_CONTROLLER = Path(__file__).resolve().parents[1]
_APP = _CONTROLLER / "app"
_BRIDGE = _CONTROLLER.parent / "scripts" / "codex_host_bridge.py"
SENTINEL_NAME = "AUTO_BROWSER_W2_27_SENTINEL"
SENTINEL = "sentinel-controller-secret-w2-27"

# Run in a fresh interpreter. A child with an empty environment reads this process's environ before and after the
# hardening call; the process then reads its own /proc/self. Prints one JSON object of statuses.
_PROBE = r"""
import json, os, subprocess, sys

CHILD = (
    "import os, sys\n"
    "try:\n"
    "    data = open('/proc/%d/environ' % os.getppid(), 'rb').read()\n"
    "except PermissionError:\n"
    "    print('denied')\n"
    "except OSError:\n"
    "    print('error')\n"
    "else:\n"
    "    print('found' if sys.argv[1].encode() in data else 'missing')\n"
)

def child_read():
    done = subprocess.run([sys.executable, "-I", "-S", "-c", CHILD, os.environ[sys.argv[2]]],
                          env={}, capture_output=True, text=True, timeout=60)
    return done.stdout.strip() or "error"

def has_cap_sys_ptrace():
    for line in open("/proc/self/status"):
        if line.startswith("CapEff:"):
            return bool(int(line.split()[1], 16) >> 19 & 1)
    return False

sys.path.insert(0, sys.argv[3])
before = child_read()
if sys.argv[1] == "controller":
    from app.process_hardening import make_non_dumpable
else:
    import importlib.util
    spec = importlib.util.spec_from_file_location("bridge_under_probe", sys.argv[4])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    make_non_dumpable = module.make_non_dumpable
took = make_non_dumpable()
after = child_read()
try:
    own = "found" if os.environ[sys.argv[2]].encode() in open("/proc/self/environ", "rb").read() else "missing"
except PermissionError:
    own = "denied"
status = "ok" if open("/proc/self/status").read().startswith("Name:") else "missing"
print(json.dumps({"before": before, "took": bool(took), "after": after, "own": own, "status": status,
                  "root": os.geteuid() == 0, "ptrace": has_cap_sys_ptrace()}))
"""


def _probe(kind: str) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), SENTINEL_NAME: SENTINEL}
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, kind, SENTINEL_NAME, str(_CONTROLLER), str(_BRIDGE)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise AssertionError(f"probe exited {done.returncode}")  # its stderr may echo the environment
    return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_DUMPABLE and /proc/PID/environ are Linux-only")
class NonDumpableProbeTests(unittest.TestCase):
    def _check(self, kind: str) -> None:
        result = _probe(kind)
        if result["before"] != "found":
            self.skipTest(f"a child cannot read its parent's environ here at all ({result['before']})")
        self.assertTrue(result["took"])
        self.assertEqual(result["status"], "ok", "the process must still read its own /proc/self")
        # A non-dumpable process's /proc/PID/environ (mode 0400) turns root-owned, so a non-root process can no longer
        # open its own either (measured as uid 65534); nothing in the controller reads it. Root still can.
        self.assertIn(result["own"], {"found"} if result["root"] else {"found", "denied"})
        if result["ptrace"]:
            self.skipTest("this process holds CAP_SYS_PTRACE, which reads a non-dumpable process anyway")
        self.assertEqual(result["after"], "denied")

    def test_a_same_uid_child_cannot_read_the_controller_environ_after_the_call(self) -> None:
        self._check("controller")

    @unittest.skipUnless(_BRIDGE.is_file(), "scripts/ is not shipped in the controller image")
    def test_a_same_uid_child_cannot_read_the_bridge_environ_after_the_call(self) -> None:
        self._check("bridge")


@unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_DUMPABLE is Linux-only")
class NonDumpableFailureTests(unittest.TestCase):
    def test_a_failing_prctl_logs_one_warning_and_goes_on(self) -> None:
        from app import process_hardening

        from . import REAL_MAKE_NON_DUMPABLE

        libc = mock.Mock()
        libc.prctl.return_value = -1
        with (
            patch.object(process_hardening.ctypes, "CDLL", return_value=libc),
            patch.object(process_hardening.ctypes, "get_errno", return_value=1),
            self.assertLogs("app.process_hardening", level="WARNING") as logs,
        ):
            self.assertFalse(REAL_MAKE_NON_DUMPABLE())
        self.assertEqual(len(logs.records), 1)
        self.assertIn("non-dumpable", logs.output[0])

    def test_a_libc_without_prctl_logs_one_warning_and_goes_on(self) -> None:
        from app import process_hardening

        from . import REAL_MAKE_NON_DUMPABLE

        with (
            patch.object(process_hardening.ctypes, "CDLL", return_value=object()),
            self.assertLogs("app.process_hardening", level="WARNING") as logs,
        ):
            self.assertFalse(REAL_MAKE_NON_DUMPABLE())
        self.assertEqual(len(logs.records), 1)


class LifespanHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_lifespan_makes_the_controller_non_dumpable_before_anything_else(self) -> None:
        from app import main

        order: list[str] = []

        class _Stop(Exception):
            pass

        def credential(_settings):
            order.append("credential")
            raise _Stop

        with (
            patch("app.process_hardening.make_non_dumpable", side_effect=lambda: order.append("dumpable")),
            patch.object(main, "ensure_api_credential", side_effect=credential),
        ):
            with self.assertRaises(_Stop):
                async with main.lifespan(main.app):
                    pass
        self.assertEqual(order, ["dumpable", "credential"])

    @unittest.skipUnless(_BRIDGE.is_file(), "scripts/ is not shipped in the controller image")
    def test_the_bridge_main_makes_itself_non_dumpable_before_anything_else(self) -> None:
        from .test_codex_host_bridge_hardening import bridge

        order: list[str] = []

        class _Stop(Exception):
            pass

        def parse_args():
            order.append("parse_args")
            raise _Stop

        with (
            patch.object(bridge, "make_non_dumpable", side_effect=lambda: order.append("dumpable")),
            patch.object(bridge, "parse_args", side_effect=parse_args),
            patch.object(bridge, "prepare_socket_path", side_effect=lambda _path: order.append("socket")),
            patch.object(bridge.os, "umask", side_effect=lambda _mask: order.append("umask")),
        ):
            with self.assertRaises(_Stop):
                bridge.main()
        self.assertEqual(order, ["dumpable", "parse_args"])


def _transport_module():
    import playwright._impl._transport as transport

    return transport


class _UnwrappedDriverEnv:
    """The original get_driver_env in place for the block; whatever was there before is put back after."""

    def __enter__(self):
        self.transport = _transport_module()
        self.found = self.transport.get_driver_env
        self.transport.get_driver_env = getattr(self.found, "__wrapped__", self.found)
        return self.transport

    def __exit__(self, *_exc) -> None:
        self.transport.get_driver_env = self.found


class PlaywrightDriverEnvironmentTests(unittest.TestCase):
    def test_the_confined_driver_env_drops_secrets_and_keeps_what_the_driver_needs(self) -> None:
        from app.process_hardening import confine_playwright_driver_environment

        env = {
            "PATH": os.environ.get("PATH", ""),
            "LC_ALL": "C",
            "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers",
            "DISPLAY": ":0",
            SENTINEL_NAME: SENTINEL,
        }
        with _UnwrappedDriverEnv() as transport, patch.dict(os.environ, env, clear=True):
            original = transport.get_driver_env
            confine_playwright_driver_environment()
            confined = transport.get_driver_env
            confine_playwright_driver_environment()
            self.assertIs(transport.get_driver_env, confined, "a second call must not wrap again")
            self.assertIs(confined.__wrapped__, original)
            driver_env = confined()
        self.assertNotIn(SENTINEL_NAME, driver_env)
        for name in ("PATH", "LC_ALL", "PLAYWRIGHT_BROWSERS_PATH", "DISPLAY", "PW_LANG_NAME"):
            self.assertIn(name, driver_env)

    def test_a_playwright_without_get_driver_env_fails_closed(self) -> None:
        from app.process_hardening import confine_playwright_driver_environment

        stub = type(sys)("playwright._impl._transport")
        with patch.dict(sys.modules, {"playwright._impl._transport": stub}):
            with self.assertRaises(RuntimeError) as raised:
                confine_playwright_driver_environment()
        self.assertIn("Playwright", str(raised.exception))


def _driver_pids() -> list[int]:
    """This process's children whose command line holds run-driver (Playwright's node driver)."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_line = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if int(stat_line.rsplit(")", 1)[1].split()[1]) == os.getpid() and b"run-driver" in cmdline:
            pids.append(int(entry.name))
    return pids


@unittest.skipUnless(sys.platform.startswith("linux"), "/proc/PID/environ is Linux-only")
class PlaywrightDriverEnvironmentProbeTests(unittest.IsolatedAsyncioTestCase):
    async def _driver_environ(self) -> dict[str, str]:
        """Start Playwright, read its driver's /proc/PID/environ as name -> 'found'/'missing' for two names, stop."""
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            pids = _driver_pids()
            self.assertEqual(len(pids), 1, "expected one Playwright driver child")
            data = Path(f"/proc/{pids[0]}/environ").read_bytes()
        finally:
            await playwright.stop()
        names = {item.split(b"=", 1)[0].decode() for item in data.split(b"\0") if item}
        return {name: ("found" if name in names else "missing") for name in (SENTINEL_NAME, "PW_LANG_NAME")}

    async def test_the_driver_holds_no_controller_secret_once_confined(self) -> None:
        from app.process_hardening import confine_playwright_driver_environment

        with _UnwrappedDriverEnv(), patch.dict(os.environ, {SENTINEL_NAME: SENTINEL}):
            before = await self._driver_environ()
            self.assertEqual(before[SENTINEL_NAME], "found", "the probe must see the sentinel unconfined")
            confine_playwright_driver_environment()
            after = await self._driver_environ()
        self.assertEqual(after, {SENTINEL_NAME: "missing", "PW_LANG_NAME": "found"})


class BrowserManagerConfinesTheDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_confines_the_driver_environment_before_starting_playwright(self) -> None:
        from app import browser_manager
        from app.browser_manager import BrowserManager

        from .test_browser_manager_create_session import _settings

        order: list[str] = []

        class _Stop(Exception):
            pass

        class _Starter:
            async def start(self):
                order.append("start")
                raise _Stop

        with tempfile.TemporaryDirectory() as tempdir:
            manager = BrowserManager(_settings(Path(tempdir)))
            with (
                patch(
                    "app.process_hardening.confine_playwright_driver_environment",
                    side_effect=lambda: order.append("confine"),
                ),
                patch.object(browser_manager, "async_playwright", return_value=_Starter()),
            ):
                with self.assertRaises(_Stop):
                    await manager.startup()
        self.assertEqual(order, ["confine", "start"])


def _spawn_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"create_subprocess_exec", "create_subprocess_shell", "Popen", "run", "check_output", "call"}:
            owner = func.value if isinstance(func, ast.Attribute) else None
            owner_name = getattr(owner, "id", "") or getattr(owner, "attr", "")
            if name in {"run", "check_output", "call"} and owner_name != "subprocess":
                continue
            yield node


class EveryChildGetsAnEnvironmentTests(unittest.TestCase):
    def test_every_subprocess_call_in_the_controller_passes_env(self) -> None:
        missing: list[str] = []
        found = 0
        for path in sorted(_APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call in _spawn_calls(tree):
                found += 1
                if not any(keyword.arg == "env" for keyword in call.keywords):
                    missing.append(f"{path.relative_to(_CONTROLLER)}:{call.lineno}")
        self.assertGreaterEqual(found, 4)  # the CLIs, the uv verifier, the regex worker, autossh
        self.assertEqual(missing, [], "a child the controller starts must get an allowlisted env=")


CONTRACT = TaskContract(
    id="uv-env",
    goal="uv gets an allowlisted environment",
    postconditions=[{"kind": "url_contains", "value": "example.com"}],
)
TRACE = {"final_observation": {"url": "https://example.com"}}
_REPORT_ENV = (
    "import json, os; names = set(os.environ);"
    " print(json.dumps({'passed': True, 'confidence': 0.5, 'failed_postconditions': sorted(names)}))"
)


class UvVerifierEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_uv_command_gets_the_base_names_and_its_passthrough_only(self) -> None:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LC_ALL": "C",
            SENTINEL_NAME: SENTINEL,
            "UV_EXTRA_SETTING": "sentinel-passed-through",
            "AUTOSSH_POLL": "30",
        }
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        adapter = UniversalVerifierAdapter(
            command=[sys.executable, "-c", _REPORT_ENV], timeout_seconds=30, env_passthrough=["uv_extra_setting"]
        )
        with patch.dict(os.environ, env, clear=True):
            result = await adapter.verify(CONTRACT, TRACE)
        names = {name.upper() for name in result.failed_postconditions}
        self.assertNotIn(SENTINEL_NAME, names)
        self.assertNotIn("AUTOSSH_POLL", names)  # autossh's own tuning reaches autossh only
        self.assertIn("UV_EXTRA_SETTING", names)
        self.assertIn("LC_ALL", names)
        self.assertIn("PATH", names)

    def test_harness_uv_env_passthrough_reaches_the_adapter(self) -> None:
        from app.startup.extensions import _build_harness_verifier

        settings = Settings(
            _env_file=None,
            HARNESS_VERIFIER="uv",
            HARNESS_UV_COMMAND="uv-verify --strict",
            HARNESS_UV_ENV_PASSTHROUGH="UV_ONE, UV_TWO",
        )
        verifier = _build_harness_verifier(settings)
        self.assertEqual(verifier.env_passthrough, ("UV_ONE", "UV_TWO"))


class RegexWorkerEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_regex_worker_gets_a_minimal_environment(self) -> None:
        from app.harness.verifier import programmatic

        seen: list[dict] = []
        original = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            seen.append(kwargs.get("env"))
            return await original(*args, **kwargs)

        with (
            patch.dict(os.environ, {**os.environ, SENTINEL_NAME: SENTINEL}, clear=True),
            mock.patch("app.harness.verifier.programmatic.asyncio.create_subprocess_exec", spy),
        ):
            results = await programmatic._regex_search_many(["b+"], "abbc", timeout=30)
        self.assertEqual(results, [True])
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], dict)
        self.assertLessEqual(set(seen[0]), {"SYSTEMROOT"})


class AutosshEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_autossh_gets_the_base_names_and_its_own_autossh_variables_only(self) -> None:
        from app.session_tunnel import IsolatedSessionTunnelBroker

        from .test_session_tunnel import FakeProcess

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "id_ed25519").write_text("dummy", encoding="utf-8")
            (root / "known_hosts").write_text("dummy", encoding="utf-8")
            settings = Settings(
                _env_file=None,
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(root / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(root / "known_hosts"),
                ISOLATED_TUNNEL_INFO_ROOT=str(root / "sessions"),
                ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
                ISOLATED_TUNNEL_REMOTE_PORT_END=16181,
                ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=0,
            )
            broker = IsolatedSessionTunnelBroker(settings)
            await broker.startup()
            seen: list = []

            async def fake_exec(*args, **kwargs):
                seen.append(kwargs.get("env"))
                return FakeProcess()

            env = {
                "PATH": "/usr/bin",
                "HOME": str(root),
                "LC_ALL": "C",
                "AUTOSSH_POLL": "30",
                SENTINEL_NAME: SENTINEL,
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec),
            ):
                tunnel = await broker.provision("session-env", local_host="browser-session-env", local_port=6080)
            await broker.release(tunnel)
        self.assertEqual(seen, [{"PATH": "/usr/bin", "HOME": str(root), "LC_ALL": "C", "AUTOSSH_POLL": "30"}])


if __name__ == "__main__":
    unittest.main()
