"""The Codex host bridge gives Codex an allowlisted environment, not the whole shell's (R-23).

The bridge runs on the host from the operator's shell and started Codex with no ``env=``, so Codex, and every
command a prompt-injected Codex runs, got every variable of that shell. It now passes the names the controller's
own provider CLIs get (``cli_environment``: CLI_BASE_ENV_NAMES, the LC_ prefix and the OpenAI adapter's
cli_env_names), plus each ``--pass-env NAME``.
"""

from __future__ import annotations

import importlib.util
import io
import os
import socketserver
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_host_bridge.py"


def _load_bridge():
    """Load the script under a private name; UnixStreamServer is missing on Windows (import only)."""
    shimmed = not hasattr(socketserver, "UnixStreamServer")
    if shimmed:
        socketserver.UnixStreamServer = socketserver.TCPServer
    try:
        spec = importlib.util.spec_from_file_location("codex_host_bridge_env_under_test", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if shimmed:
            del socketserver.UnixStreamServer


bridge = _load_bridge() if _SCRIPT.is_file() else None
API_KEY = "sk-sentinel-openai-key"
UNRELATED = "sentinel-unrelated-secret"


class _RecordingPopen:
    """Stands in for subprocess.Popen and records the environment it was given."""

    calls: list[dict] = []

    def __init__(self, command, **kwargs) -> None:
        type(self).calls.append(kwargs)
        self.returncode = 0
        self.pid = 0

    def communicate(self, input=None, timeout=None):
        return b"", b""


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class CodexEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingPopen.calls = []

    def _env_given(self, service, environ: dict[str, str]) -> dict[str, str]:
        with patch.dict(os.environ, environ, clear=True), patch.object(bridge.subprocess, "Popen", _RecordingPopen):
            service._run_codex(["codex", "exec", "-"], b"prompt")
        self.assertEqual(len(_RecordingPopen.calls), 1)
        env = _RecordingPopen.calls[0].get("env")
        self.assertIsInstance(env, dict)
        return env

    def test_codex_gets_the_allowlist_and_not_an_unrelated_secret(self) -> None:
        environ = {"PATH": "/usr/bin", "HOME": "/home/op", "OPENAI_API_KEY": API_KEY, "UNRELATED_TOKEN": UNRELATED}

        env = self._env_given(bridge.CodexBridgeService(codex_path="codex"), environ)

        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/home/op", "OPENAI_API_KEY": API_KEY})

    def test_names_compare_as_cli_environment_compares_them(self) -> None:
        environ = {"Path": "C:\\bin", "LC_ALL": "C", "lc_ctype": "C", "codex_home": "/c", "LCX": "no"}

        env = self._env_given(bridge.CodexBridgeService(codex_path="codex"), environ)

        expected = {"Path": "C:\\bin", "LC_ALL": "C", "lc_ctype": "C", "codex_home": "/c"}
        if os.name == "nt":
            # W2-28: Windows' os.environ upper-cases every name it stores, so the case cannot be kept there; the
            # names still have to pass as cli_environment compares them.
            env = {name.upper(): value for name, value in env.items()}
            expected = {name.upper(): value for name, value in expected.items()}
        self.assertEqual(env, expected)

    def test_codex_ca_certificate_passes(self) -> None:
        # W2-30: Codex's own CA bundle variable, read before SSL_CERT_FILE; a path, not a secret.
        environ = {"PATH": "/usr/bin", "CODEX_CA_CERTIFICATE": "/etc/ssl/corp-ca.pem"}

        env = self._env_given(bridge.CodexBridgeService(codex_path="codex"), environ)

        self.assertEqual(env, environ)

    def test_pass_env_adds_exactly_the_named_variable(self) -> None:
        environ = {"PATH": "/usr/bin", "MY_PROXY_HINT": "on", "OTHER_SECRET": UNRELATED}

        env = self._env_given(bridge.CodexBridgeService(codex_path="codex", pass_env=("MY_PROXY_HINT",)), environ)

        self.assertEqual(env, {"PATH": "/usr/bin", "MY_PROXY_HINT": "on"})

    def test_the_copy_matches_the_controllers_sets(self) -> None:
        from app.providers.base import CLI_BASE_ENV_NAMES, CLI_BASE_ENV_PREFIXES
        from app.providers.openai_adapter import OpenAIAdapter

        self.assertEqual(bridge.CODEX_BASE_ENV_NAMES, CLI_BASE_ENV_NAMES)
        self.assertEqual(bridge.CODEX_BASE_ENV_PREFIXES, CLI_BASE_ENV_PREFIXES)
        self.assertEqual(bridge.CODEX_PROVIDER_ENV_NAMES, tuple(OpenAIAdapter.cli_env_names))


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class PassEnvOptionTests(unittest.TestCase):
    def _main(self, *argv: str) -> tuple[int, str, Mock]:
        service = Mock(side_effect=RuntimeError("stop here"))
        captured = io.StringIO()
        with (
            patch.object(sys, "argv", ["codex_host_bridge.py", *argv]),
            patch.object(bridge, "prepare_socket_path", Mock(return_value=Path("unused.sock"))) as prepare,
            patch.object(bridge, "CodexBridgeService", service),
            patch.object(bridge.os, "umask"),  # main() sets 0o077; left set, it changed later tests' file modes
            patch.object(bridge, "make_non_dumpable"),  # left set, the runner would stay non-dumpable
            redirect_stderr(captured),
        ):
            try:
                code = bridge.main()
            except RuntimeError:
                code = None
        self.prepare = prepare
        return code, captured.getvalue(), service

    def test_the_option_repeats_and_reaches_the_service(self) -> None:
        code, _, service = self._main("--pass-env", "MY_A", "--pass-env", "_B2")

        self.assertIsNone(code)  # reached the service
        self.assertEqual(tuple(service.call_args.kwargs["pass_env"]), ("MY_A", "_B2"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_GET_DUMPABLE is Linux-only")
    def test_main_leaves_the_test_runner_dumpable(self) -> None:
        # main() makes the bridge non-dumpable first (W2-27); run in this process, it must not flip the runner.
        import ctypes

        self._main()
        self.assertEqual(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), 1)  # PR_GET_DUMPABLE

    def test_no_option_passes_no_extra_name(self) -> None:
        _, _, service = self._main()

        self.assertEqual(tuple(service.call_args.kwargs["pass_env"]), ())

    def test_a_bad_name_exits_2_at_startup_with_a_clear_message(self) -> None:
        for bad in ("1ABC", "A-B", "A B", "", "A=B", "Ä"):
            with self.subTest(name=bad):
                code, stderr, service = self._main("--pass-env", bad)

                self.assertEqual(code, 2)
                self.assertIn("--pass-env", stderr)
                self.assertIn("letters, digits and underscores", stderr)
                service.assert_not_called()
                self.prepare.assert_not_called()

    def test_the_help_says_the_value_reaches_codex_and_its_commands(self) -> None:
        captured = io.StringIO()
        with patch.object(sys, "argv", ["codex_host_bridge.py", "--help"]), patch("sys.stdout", captured):
            with self.assertRaises(SystemExit):
                bridge.parse_args()
        text = " ".join(captured.getvalue().split())
        self.assertIn("--pass-env NAME", text)
        self.assertIn("every command Codex runs", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
