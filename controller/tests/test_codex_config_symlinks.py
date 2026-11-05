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
        self.config = home / ".codex" / "config.toml"
        self.config.write_text(
            'a = "/home/alice/.codex/auth.json"\n'
            'b = "/etc/.codex/x"\n'
            '[nested]\nc = "/root/.codex/y"\nd = "/home/alice/../../srv/.codex/z"\n',
            encoding="utf-8",
        )
        self.adapter = OpenAIAdapter(Settings(_env_file=None, OPENAI_API_KEY="k", CLI_HOME=str(home)))
        self.made: list[Path] = []
        self.linked: list[Path] = []
        # Nothing outside the temp CLI home is looked at for real: such a path is recorded and reported missing, or
        # refused with EACCES, as Python 3.11 and 3.12 raise for /root/.codex when not root.
        self.probed: list[Path] = []
        self.denied: set[Path] = set()
        real_exists = Path.exists

        def exists(path: Path, *args, **kwargs) -> bool:
            if path == home or home in path.parents:
                return real_exists(path, *args, **kwargs)
            self.probed.append(path)
            if path in self.denied:
                raise PermissionError(13, "Permission denied", str(path))
            return False

        fakes = {
            "mkdir": lambda self_path, *a, **kw: self.made.append(self_path),
            "symlink_to": lambda self_path, *a, **kw: self.linked.append(self_path),
            "exists": exists,
        }
        for name, fake in fakes.items():
            patcher = patch.object(Path, name, fake)
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

    def test_a_refused_root_is_logged_without_being_looked_at(self) -> None:
        # STORE-35: exists() ran before the place check, so a refused /root/.codex raised PermissionError out of the
        # first CLI call for a non-root controller, and every root sorted after it was never handled.
        self.denied = {Path("/root/.codex"), Path("/etc/.codex")}

        with self.assertLogs("app.providers.openai_adapter", level="WARNING") as captured:
            self.adapter._ensure_codex_config_path_compatibility()

        self.assertEqual(self.probed, [Path("/home/alice/.codex")])
        self.assertEqual(self.linked, [Path("/home/alice/.codex")])
        self.assertIn("/root/.codex", "\n".join(captured.output))

    def test_an_allowed_root_that_cannot_be_checked_is_logged_and_the_next_one_is_still_linked(self) -> None:
        self.config.write_text('a = "/home/alice/.codex/x"\nb = "/home/bob/.codex/y"\n', encoding="utf-8")
        self.denied = {Path("/home/alice/.codex")}

        with self.assertLogs("app.providers.openai_adapter", level="WARNING") as captured:
            self.adapter._ensure_codex_config_path_compatibility()

        self.assertEqual(self.linked, [Path("/home/bob/.codex")])
        self.assertIn("/home/alice/.codex", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
