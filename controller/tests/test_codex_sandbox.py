"""Codex must not run on the host with approvals and sandboxing disabled.

GHSA-xmh3-cw7j-9gp5. Both Codex paths passed
`--dangerously-bypass-approvals-and-sandbox`, a flag whose own help text calls it
"EXTREMELY DANGEROUS. Intended solely for running in environments that are
externally sandboxed". Neither path needs it: Codex is handed a screenshot and a
prompt in an ephemeral temp directory and asked for a JSON decision.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from app.codex_sandbox import BYPASS_FLAG, build_sandbox_flags, codex_sandbox_flags
from app.config import Settings

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

ADAPTER_SOURCE = Path(__file__).resolve().parents[1] / "app" / "providers" / "openai_adapter.py"


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


if __name__ == "__main__":
    unittest.main()
