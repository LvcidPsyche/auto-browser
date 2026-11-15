"""The codex config compatibility shim creates symlinks only in safe places, once (STORE-35).

A config.toml under the CLI home can name any absolute `/.../.codex/...` path; the shim used to make the parent
directories and a symlink for every one of them, anywhere the controller could write.

The CLI writes its home, and the controller is not under the CLI sandbox: the shim reads config.toml only as a regular
file that is itself there. The FIFO and link cases need POSIX.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

from app.config import Settings
from app.providers import openai_adapter
from app.providers.base import CLI_STDOUT_LIMIT_BYTES
from app.providers.openai_adapter import OpenAIAdapter
from tests.test_codex_call_files import _warnings, posix_only

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

    def test_a_root_that_cannot_be_linked_is_logged_and_the_next_one_is_still_linked(self) -> None:
        # Python 3.14's exists() returns False on EACCES, so an uncheckable root reaches mkdir; a non-root controller
        # under a root-owned /home fails there too. Both were skipped without a word.
        self.config.write_text('a = "/home/alice/.codex/x"\nb = "/home/bob/.codex/y"\n', encoding="utf-8")

        def mkdir(path: Path, *args, **kwargs) -> None:
            if path == Path("/home/alice"):
                raise PermissionError(13, "Permission denied", str(path))
            self.made.append(path)

        with (
            patch.object(Path, "mkdir", mkdir),
            self.assertLogs("app.providers.openai_adapter", level="WARNING") as captured,
        ):
            self.adapter._ensure_codex_config_path_compatibility()

        self.assertEqual(self.linked, [Path("/home/bob/.codex")])
        self.assertIn("/home/alice/.codex", "\n".join(captured.output))
        self.assertIn("PermissionError", "\n".join(captured.output))

    def test_a_root_with_a_nul_byte_is_logged_and_the_next_one_is_still_linked(self) -> None:
        # exists() answers False for a path the OS cannot take, and mkdir then raises ValueError, not OSError.
        self.config.write_text('a = "/home/a\\u0000b/.codex/x"\nb = "/home/bob/.codex/y"\n', encoding="utf-8")

        def mkdir(path: Path, *args, **kwargs) -> None:
            if "\x00" in str(path):
                raise ValueError("embedded null byte")
            self.made.append(path)

        with (
            patch.object(Path, "mkdir", mkdir),
            self.assertLogs("app.providers.openai_adapter", level="WARNING") as captured,
        ):
            self.adapter._ensure_codex_config_path_compatibility()

        self.assertEqual(self.linked, [Path("/home/bob/.codex")])
        self.assertIn("ValueError", "\n".join(captured.output))


@pytest.fixture
def cli_home(tmp_path: Path) -> Path:
    home = tmp_path / "cli-home"
    (home / ".codex").mkdir(parents=True)
    return home


def _shim(cli_home: Path) -> OpenAIAdapter:
    return OpenAIAdapter(Settings(_env_file=None, OPENAI_API_KEY="k", CLI_HOME=str(cli_home)))


@posix_only
def test_a_config_that_is_a_fifo_is_not_waited_on(cli_home: Path, caplog) -> None:
    # A Codex call runs the shim on the event loop's thread: a read waiting for a FIFO's writer held every request.
    fifo = cli_home / ".codex" / "config.toml"
    os.mkfifo(fifo)
    shim = threading.Thread(target=_shim(cli_home)._ensure_codex_config_path_compatibility, daemon=True)
    with caplog.at_level(logging.WARNING):
        shim.start()
        shim.join(2)
        blocked = shim.is_alive()
        if blocked:
            # Open the write end, so the waiting read ends and so does the run that failed. ENXIO: it ended meanwhile.
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            shim.join(5)

    assert not blocked, "the shim waited for a writer on the FIFO"
    (line,) = _warnings(caplog)
    assert "a FIFO" in line


@posix_only
def test_a_config_that_is_a_link_is_not_read_and_said_once(cli_home: Path, tmp_path: Path, caplog) -> None:
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(f'notify = ["{cli_home.as_posix()}/sub/.codex/hook"]\n', encoding="utf-8")
    (cli_home / ".codex" / "config.toml").symlink_to(elsewhere)
    adapter = _shim(cli_home)

    with caplog.at_level(logging.WARNING):
        adapter._ensure_codex_config_path_compatibility()
        adapter._ensure_codex_config_path_compatibility()

    assert not (cli_home / "sub").exists()
    (line,) = _warnings(caplog)
    assert "a link" in line


def test_a_config_larger_than_the_limit_is_not_read(cli_home: Path, caplog) -> None:
    (cli_home / ".codex" / "config.toml").write_bytes(b"#" * (CLI_STDOUT_LIMIT_BYTES + 1))

    with (
        patch.object(openai_adapter.tomllib, "loads", wraps=openai_adapter.tomllib.loads) as parse,
        caplog.at_level(logging.WARNING),
    ):
        _shim(cli_home)._ensure_codex_config_path_compatibility()

    assert parse.call_count == 0, "the config was parsed"
    (line,) = _warnings(caplog)
    assert str(CLI_STDOUT_LIMIT_BYTES) in line


def _naming(cli_home: Path, root: str) -> None:
    (cli_home / ".codex" / "config.toml").write_text(
        f'notify = ["{cli_home.as_posix()}/{root}/hook"]\n', encoding="utf-8"
    )


@posix_only
def test_a_nested_root_in_the_cli_home_is_still_linked(cli_home: Path) -> None:
    _naming(cli_home, "work/project/.codex")

    _shim(cli_home)._ensure_codex_config_path_compatibility()

    made = cli_home / "work" / "project" / ".codex"
    assert made.is_symlink() and os.readlink(made) == str(cli_home / ".codex")


@posix_only
def test_a_root_through_a_link_in_the_cli_home_makes_nothing_where_it_leads(
    cli_home: Path, tmp_path: Path, caplog
) -> None:
    # CLI_HOME/x -> /data/auth, as a CLI may plant it in its own home: the controller, not under the sandbox, made the
    # directories and the link in the place the CLI cannot write.
    protected = tmp_path / "protected"
    protected.mkdir()
    (cli_home / "x").symlink_to(protected, target_is_directory=True)
    _naming(cli_home, "x/planted/deep/.codex")

    with caplog.at_level(logging.WARNING):
        _shim(cli_home)._ensure_codex_config_path_compatibility()

    assert list(protected.iterdir()) == []
    (line,) = _warnings(caplog)
    assert "x/planted/deep/.codex" in line and "'x' is a link" in line


@posix_only
def test_a_part_put_back_as_a_link_after_it_was_made_is_not_followed(cli_home: Path, tmp_path: Path, caplog) -> None:
    # A CLI working in its home meanwhile can rename the directory just made aside and leave a link in its place.
    protected = tmp_path / "protected"
    protected.mkdir()
    _naming(cli_home, "sub/deep/.codex")
    real_mkdir = os.mkdir
    swapped: list[str] = []

    def mkdir(path, *args, **kwargs) -> None:
        real_mkdir(path, *args, **kwargs)
        if os.path.basename(os.fspath(path)) == "sub" and not swapped:
            swapped.append(os.fspath(path))
            os.rename(cli_home / "sub", cli_home / "sub-aside")
            (cli_home / "sub").symlink_to(protected, target_is_directory=True)

    with patch.object(os, "mkdir", mkdir), caplog.at_level(logging.WARNING):
        _shim(cli_home)._ensure_codex_config_path_compatibility()

    assert swapped, "the directory was never made"
    assert list(protected.iterdir()) == []
    (line,) = _warnings(caplog)
    assert "sub/deep/.codex" in line and "'sub' is a link" in line


if __name__ == "__main__":
    unittest.main()
