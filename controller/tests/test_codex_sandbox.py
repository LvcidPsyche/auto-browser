"""Codex must not run on the host with approvals and sandboxing disabled.

GHSA-xmh3-cw7j-9gp5. Both Codex paths passed
`--dangerously-bypass-approvals-and-sandbox`, a flag whose own help text calls it
"EXTREMELY DANGEROUS. Intended solely for running in environments that are
externally sandboxed". Neither path needs it: Codex is handed a screenshot and a
prompt in an ephemeral temp directory and asked for a JSON decision.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.codex_sandbox import BYPASS_FLAG, build_sandbox_flags, codex_sandbox_flags
from app.config import Settings
from app.providers.base import CLIResult
from app.providers.openai_adapter import OpenAIAdapter
from tests._codex_mcp_list import answers_mcp_list, listing_no_servers
from tests.test_codex_host_bridge_hardening import bridge

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

ADAPTER_SOURCE = Path(__file__).resolve().parents[1] / "app" / "providers" / "openai_adapter.py"
DECISION = '{"action":"done","reason":"r","risk_category":"read"}'
# Each sandbox setting as the adapter reads it and as the bridge is given it, and the flags `codex exec` gets for it.
EXEC_CASES = (
    ({}, {}, ["-s", "read-only"]),
    ({"CODEX_SANDBOX_MODE": "workspace-write"}, {"sandbox_mode": "workspace-write"}, ["-s", "workspace-write"]),
    ({"CODEX_BRIDGE_ALLOW_HOST_EXEC": "true"}, {"allow_host_exec": True}, [BYPASS_FLAG]),
)


def _sandbox_words(command: list[str]) -> list[str]:
    """Every word of ``command`` that sets Codex's sandbox, wherever it stands: each -s or --sandbox with its value,
    a -c override of sandbox_mode, and the bypass flag. A later one overrides an earlier, so a second one anywhere on
    the line counts."""
    words: list[str] = []
    for at, word in enumerate(command):
        value = command[at + 1] if at + 1 < len(command) else ""
        if word in ("-s", "--sandbox") or (word == "-c" and value.startswith("sandbox")):
            words += [word, value]
        elif word == BYPASS_FLAG or word.startswith("--sandbox="):
            words.append(word)
    return words


class SandboxFlagTests(unittest.TestCase):
    def test_the_default_is_a_read_only_sandbox(self) -> None:
        self.assertEqual(codex_sandbox_flags(Settings(_env_file=None)), ["-s", "read-only"])

    def test_the_bypass_requires_an_explicit_opt_in(self) -> None:
        settings = Settings(_env_file=None, CODEX_BRIDGE_ALLOW_HOST_EXEC="true")
        self.assertEqual(codex_sandbox_flags(settings), [BYPASS_FLAG])

    def test_an_operator_may_widen_the_sandbox_without_disabling_it(self) -> None:
        settings = Settings(_env_file=None, CODEX_SANDBOX_MODE="workspace-write")
        self.assertEqual(codex_sandbox_flags(settings), ["-s", "workspace-write"])

    def test_an_unknown_mode_is_refused_rather_than_passed_through(self) -> None:
        with self.assertRaises(ValueError):
            build_sandbox_flags(allow_host_exec=False, sandbox_mode="wide-open")

    def test_the_adapter_no_longer_hardcodes_the_bypass(self) -> None:
        """The flag must reach the command line only through the helper."""
        self.assertNotIn(BYPASS_FLAG, ADAPTER_SOURCE.read_text(encoding="utf-8"))


class HostBridgeTests(unittest.TestCase):
    """The bridge is a standalone script, so it carries its own copy of the rule."""

    def setUp(self) -> None:
        try:
            import codex_host_bridge
        except Exception as exc:  # pragma: no cover - platform dependent
            raise unittest.SkipTest(f"codex_host_bridge unavailable: {exc}") from exc
        self.module = codex_host_bridge

    def service(self, **kwargs):
        return self.module.CodexBridgeService(codex_path="codex", **kwargs)

    def test_the_bridge_defaults_to_a_read_only_sandbox(self) -> None:
        self.assertEqual(self.service().sandbox_flags, ["-s", "read-only"])

    def test_the_bridge_bypass_requires_an_explicit_opt_in(self) -> None:
        self.assertEqual(
            self.service(allow_host_exec=True).sandbox_flags,
            ["--dangerously-bypass-approvals-and-sandbox"],
        )

    def test_the_two_implementations_agree(self) -> None:
        """Duplicated rule, so drift between them is what gets pinned."""
        for allow in (False, True):
            for mode in ("read-only", "workspace-write", "danger-full-access"):
                with self.subTest(allow=allow, mode=mode):
                    self.assertEqual(
                        build_sandbox_flags(allow_host_exec=allow, sandbox_mode=mode),
                        self.module.build_sandbox_flags(allow_host_exec=allow, sandbox_mode=mode),
                    )


class ExecCommandTests(unittest.IsolatedAsyncioTestCase):
    """The tests above hold the flags each path works out; these, that its `codex exec` is run with them."""

    async def test_the_adapters_codex_exec_carries_the_sandbox(self) -> None:
        commands: list[list[str]] = []

        @answers_mcp_list
        async def fake_run_cli(*, command: list[str], **_kwargs) -> CLIResult:
            commands.append(command)
            Path(command[command.index("--output-last-message") + 1]).write_text(DECISION, encoding="utf-8")
            return CLIResult(command=command, stdout="", stderr="", returncode=0)

        for env, _options, flags in EXEC_CASES:
            with self.subTest(**env), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "cli-home"
                home.mkdir()
                settings = Settings(
                    _env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=str(home), **env
                )
                adapter = OpenAIAdapter(settings)
                with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
                    await adapter._decide(
                        goal="g",
                        observation={"url": "https://example.com"},
                        context_hints=None,
                        previous_steps=[],
                        model_override=None,
                    )

                command = commands[-1]
                self.assertEqual(_sandbox_words(command), flags)
                self.assertEqual(command.count(BYPASS_FLAG), flags.count(BYPASS_FLAG))

    @unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
    def test_the_bridges_codex_exec_carries_the_sandbox(self) -> None:
        commands: list[list[str]] = []

        class FakeCodex:
            returncode = 0

            def __init__(self, command: list[str], **_kwargs) -> None:
                commands.append(command)
                Path(command[command.index("--output-last-message") + 1]).write_text(DECISION, encoding="utf-8")

            def communicate(self, _input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"", b""

        for _env, options, flags in EXEC_CASES:
            with self.subTest(**options), patch.object(bridge.subprocess, "Popen", listing_no_servers(FakeCodex)):
                service = bridge.CodexBridgeService(codex_path="codex", **options)
                service.handle_openai_decide({"prompt": "p", "schema": {"type": "object"}})

                command = commands[-1]
                self.assertEqual(_sandbox_words(command), flags)
                self.assertEqual(command.count(BYPASS_FLAG), flags.count(BYPASS_FLAG))


if __name__ == "__main__":
    unittest.main()
