"""The codex config compatibility shim creates symlinks only in safe places, once (STORE-35).

A config.toml under the CLI home can name any absolute `/.../.codex/...` path; the shim used to make the parent
directories and a symlink for every one of them, anywhere the controller could write.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from app.config import Settings
from app.providers import openai_adapter
from app.providers.openai_adapter import OpenAIAdapter

CLI_HOME = PurePosixPath("/data/cli-home")


class CodexRootAllowedTests(unittest.TestCase):
    def test_allowed_places(self) -> None:
        for path in (
            "/home/alice/.codex",
            "/data/cli-home/.codex",
            "/data/cli-home/work/project/.codex",
        ):
            with self.subTest(path=path):
                self.assertTrue(openai_adapter._codex_root_allowed(PurePosixPath(path), CLI_HOME))

    def test_refused_places(self) -> None:
        for path in (
            "/etc/.codex",
            "/root/.codex",
            "/.codex",
            "/usr/local/.codex",
            "/home/.codex",
            "/home/alice/deep/.codex",
            "/home/alice/../../etc/.codex",
            "/data/cli-home/../outside/.codex",
            "/data/other/.codex",
        ):
            with self.subTest(path=path):
                self.assertFalse(openai_adapter._codex_root_allowed(PurePosixPath(path), CLI_HOME))


class EnsureCodexConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text(
            'a = "/home/alice/.codex/auth.json"\n'
            'b = "/etc/.codex/x"\n'
            '[nested]\nc = "/root/.codex/y"\nd = "/home/alice/../../srv/.codex/z"\n',
            encoding="utf-8",
        )
        self.adapter = OpenAIAdapter(Settings(_env_file=None, OPENAI_API_KEY="k", CLI_HOME=str(home)))
        self.made: list[Path] = []
        self.linked: list[Path] = []
        for name, record in (("mkdir", self.made), ("symlink_to", self.linked)):
            patcher = patch.object(Path, name, lambda self_path, *a, _record=record, **kw: _record.append(self_path))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_the_allowed_root_is_linked_and_the_rest_is_logged(self) -> None:
        with self.assertLogs("app.providers.openai_adapter", level="WARNING") as captured:
            self.adapter._ensure_codex_config_path_compatibility()

        self.assertEqual(self.linked, [Path("/home/alice/.codex")])
        self.assertEqual(self.made, [Path("/home/alice")])
        text = "\n".join(captured.output)
        for refused in ("/etc/.codex", "/root/.codex"):
            self.assertIn(refused, text)

    def test_it_runs_once_per_adapter(self) -> None:
        with patch.object(openai_adapter.tomllib, "loads", wraps=openai_adapter.tomllib.loads) as parse:
            with self.assertLogs("app.providers.openai_adapter", level="WARNING"):
                self.adapter._ensure_codex_config_path_compatibility()
            self.adapter._ensure_codex_config_path_compatibility()
            self.adapter._ensure_codex_config_path_compatibility()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(self.linked, [Path("/home/alice/.codex")])


if __name__ == "__main__":
    unittest.main()
