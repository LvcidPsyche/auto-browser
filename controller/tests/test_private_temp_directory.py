"""The controller's own temp directory (CLI-shared-temp-dirs): made 0700 and emptied in the CLI sandbox's protected
root, given to tempfile, SQLite, Playwright's driver, Tesseract, the Universal Verifier's command and autossh
(child_environment), and closed to a CLI under the launcher."""

from __future__ import annotations

import ast
import asyncio
import errno
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from .test_process_hardening import (
    _APP,
    CONTRACT,
    TRACE,
    _autossh_environment,
    _KeptTesseractEnviron,
    _transport_module,
    _UnwrappedDriverEnv,
)

# A verifier's command that says where its tempfile puts its files.
_REPORT_TEMP = (
    "import json, sys, tempfile; sys.stdin.read();"
    " print(json.dumps({'passed': True, 'confidence': 1.0, 'notes': tempfile.gettempdir()}))"
)
# Rows enough that sorting them outgrows SQLite's sort memory (a few MB of keys) at a cache of a few pages.
_SPILLED_ROWS = 100_000


def _sqlite_temp_directory(value: str | None = None) -> str | None:
    """SQLite's temp directory for this process (PRAGMA temp_store_directory), set to ``value`` first where it is
    given ("" for SQLite's own choice)."""
    connection = sqlite3.connect(":memory:")
    try:
        if value is not None:
            connection.execute("PRAGMA temp_store_directory = '%s'" % value.replace("'", "''"))
        row = connection.execute("PRAGMA temp_store_directory").fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def _open_files() -> set[str]:
    """The files this process has open, by /proc/self/fd, as their paths (a file unlinked while open too)."""
    seen = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:  # the directory's own fd, closed by the time it is read
            continue
        if target.startswith("/"):
            seen.add(target.removesuffix(" (deleted)"))
    return seen


class _PrivateTempCase(unittest.TestCase):
    """A protected root of the CLI sandbox's in a temp directory, with a CLI home in it, for the controller's own temp
    directory (CLI-shared-temp-dirs). tempfile's directory, SQLite's and the module's record of it are put back after
    each test."""

    def setUp(self) -> None:
        from app import cli_sandbox, process_hardening

        self.process_hardening = process_hardening
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.sqlite_temp = _sqlite_temp_directory()
        self.addCleanup(_sqlite_temp_directory, self.sqlite_temp or "")
        self.root = Path(os.path.realpath(base.name)) / "data"
        self.home = self.root / "cli-home"
        self.home.mkdir(parents=True)
        self.private = self.root / process_hardening.PRIVATE_TEMP_NAME
        for patcher in (
            patch.object(cli_sandbox, "PROTECTED_ROOT", str(self.root)),
            patch.object(process_hardening, "_private_temp", None),
            patch.object(tempfile, "tempdir", tempfile.tempdir),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.system_temp = tempfile.gettempdir()

    def use(self) -> str | None:
        from . import REAL_USE_PRIVATE_TEMP_DIRECTORY

        with patch.dict(os.environ, {"TMPDIR": self.system_temp}):
            made = REAL_USE_PRIVATE_TEMP_DIRECTORY()
            self.assertEqual(os.environ["TMPDIR"], self.system_temp, "a provider CLI keeps the operator's TMPDIR")
            return made


@unittest.skipUnless(sys.platform.startswith("linux"), "the CLI sandbox, and so this directory, are Linux's")
class PrivateTempDirectoryTests(_PrivateTempCase):
    def test_the_call_makes_a_0700_directory_of_this_users_in_the_root_and_tempfile_uses_it(self) -> None:
        made = self.use()

        self.assertEqual(made, str(self.private))
        info = os.lstat(self.private)
        self.assertEqual((oct(info.st_mode & 0o7777), info.st_uid), (oct(0o700), os.geteuid()))
        self.assertEqual(tempfile.gettempdir(), str(self.private))
        with tempfile.NamedTemporaryFile() as made_file:
            self.assertEqual(Path(made_file.name).parent, self.private)

    def test_the_drivers_environment_and_tesseracts_get_the_directory_as_their_temp_directory(self) -> None:
        # B5-fix2 item 12 (H-7, H-10, H-11): each helper puts back what it found, the parent's TMPDIR, TEMP and TMP stay
        # the operator's, and a child gets TEMP and TMP only where its environment has them.
        import pytesseract.pytesseract as pytesseract

        from app.process_hardening import confine_playwright_driver_environment, confine_tesseract_environment

        from . import REAL_USE_PRIVATE_TEMP_DIRECTORY

        names = ("TMPDIR", "TEMP", "TMP")
        for env in (
            {"PATH": "/usr/bin", "TMPDIR": "/tmp", "TEMP": "/tmp", "TMP": "/var/tmp"},
            {"PATH": "/usr/bin", "TMPDIR": "/tmp"},
        ):
            with self.subTest(names=sorted(env)):
                found = (_transport_module().get_driver_env, pytesseract.environ)
                self.process_hardening._private_temp = None  # made at each, as at a start
                with (
                    _UnwrappedDriverEnv() as transport,
                    _KeptTesseractEnviron() as module,
                    patch.dict(os.environ, env, clear=True),
                ):
                    REAL_USE_PRIVATE_TEMP_DIRECTORY()
                    confine_playwright_driver_environment()
                    confine_tesseract_environment()
                    driver_env = transport.get_driver_env()
                    tesseract_env = dict(module.environ)
                    self.assertEqual(
                        {name: os.environ.get(name) for name in names}, {name: env.get(name) for name in names}
                    )

                self.assertIs(_transport_module().get_driver_env, found[0])
                self.assertIs(pytesseract.environ, found[1])
                given = {name: str(self.private) for name in names if name == "TMPDIR" or name in env}
                for confined in (driver_env, tesseract_env):
                    self.assertEqual({name: confined[name] for name in names if name in confined}, given)

    def test_the_verifiers_command_and_autossh_get_the_directory_as_their_temp_directory(self) -> None:
        # B5-fix2 item 5 (C2-4): with the operator's TMPDIR, a verifier that stages the trace it is given through
        # tempfile put it in /tmp, where a sandboxed CLI read it and rewrote it before the verifier read it back.
        from app.harness.verifier.uv import UniversalVerifierAdapter

        adapter = UniversalVerifierAdapter(command=[sys.executable, "-c", _REPORT_TEMP], timeout_seconds=30)
        env = {"PATH": os.environ.get("PATH", ""), "TMPDIR": self.system_temp}
        with patch.dict(os.environ, env, clear=True):
            self.use()
            result = asyncio.run(adapter.verify(CONTRACT, TRACE))
            started = asyncio.run(_autossh_environment(env))
            self.assertEqual(os.environ["TMPDIR"], self.system_temp, "a provider CLI keeps the operator's TMPDIR")

        self.assertEqual((result.notes, started["TMPDIR"]), (str(self.private), str(self.private)))

    def test_without_the_directory_the_driver_and_tesseract_keep_the_temp_directory_they_had(self) -> None:
        from app.process_hardening import confine_playwright_driver_environment, confine_tesseract_environment

        with (
            _UnwrappedDriverEnv() as transport,
            _KeptTesseractEnviron() as module,
            patch.dict(os.environ, {"PATH": "/usr/bin", "TMPDIR": "/tmp"}, clear=True),
        ):
            confine_playwright_driver_environment()
            confine_tesseract_environment()
            self.assertEqual((transport.get_driver_env()["TMPDIR"], module.environ["TMPDIR"]), ("/tmp", "/tmp"))

    def test_what_an_earlier_start_left_there_is_removed_and_a_second_call_keeps_what_this_one_put_there(self) -> None:
        # A start after a crash: an OCR's screenshot, a local browser's profile.
        (self.private / "playwright_chromiumdev_profile-x").mkdir(parents=True)
        self.private.chmod(0o700)
        (self.private / "tess_left_input.PNG").write_bytes(b"screenshot")
        (self.private / "a-link").symlink_to(self.home, target_is_directory=True)
        directory = os.lstat(self.private).st_ino

        self.use()
        self.assertEqual(list(self.private.iterdir()), [])
        # Emptied, not made again: what hides the directory from the host bridge's Codex still holds.
        self.assertEqual(os.lstat(self.private).st_ino, directory)
        self.assertTrue(self.home.is_dir(), "a link's target is not removed with it")
        (self.private / "in-use").write_text("this process's", encoding="utf-8")
        self.assertEqual(self.use(), str(self.private))

        self.assertEqual([path.name for path in self.private.iterdir()], ["in-use"])

    def _sqlite_keeps_its_own(self, **connect) -> str:
        """use() with sqlite3.connect patched as ``connect`` says: tempfile's directory is the private one, SQLite's
        the one it had, and the one warning, which is returned, is about SQLite's."""
        with (
            patch.object(self.process_hardening.sqlite3, "connect", **connect),
            self.assertLogs("app.process_hardening", level="WARNING") as logs,
        ):
            made = self.use()

        self.assertEqual((made, tempfile.gettempdir()), (str(self.private), str(self.private)))
        self.assertEqual(_sqlite_temp_directory(), self.sqlite_temp)
        (line,) = logs.output
        self.assertIn("SQLite's temp files", line)
        return line

    def test_a_sqlite_that_does_not_take_the_directory_keeps_its_own_with_one_warning(self) -> None:
        # A SQLite built without PRAGMA temp_store_directory ignores it as unknown, so reading it back finds no row.
        without_the_pragma = MagicMock(**{"execute.return_value.fetchone.return_value": None})

        line = self._sqlite_keeps_its_own(return_value=without_the_pragma)

        self.assertIn("this SQLite has no PRAGMA temp_store_directory", line)

    def test_a_sqlite_that_cannot_open_a_connection_keeps_its_own_with_one_warning(self) -> None:
        line = self._sqlite_keeps_its_own(side_effect=sqlite3.OperationalError("unable to open database file"))

        self.assertIn("unable to open database file", line)

    def test_the_connection_that_gives_sqlite_the_directory_is_closed_whether_sqlite_takes_it_or_not(self) -> None:
        # A real connection, kept: a with-statement on it alone commits and leaves it open.
        connect, opened = sqlite3.connect, []

        def kept(*args, **kwargs):
            opened.append(connect(*args, **kwargs))
            return opened[-1]

        # SQLite takes a directory that is there, and refuses one that is not ("not a writable directory").
        for directory, logs in ((self.home, self.assertNoLogs), (self.root / "not-there", self.assertLogs)):
            with self.subTest(directory=directory.name):
                opened.clear()
                with (
                    patch.object(self.process_hardening.sqlite3, "connect", side_effect=kept),
                    logs("app.process_hardening", level="WARNING"),
                ):
                    self.process_hardening._give_to_sqlite(str(directory))

                (connection,) = opened
                with self.assertRaises(sqlite3.ProgrammingError):  # "Cannot operate on a closed database."
                    connection.execute("SELECT 1")

    def _refused(self) -> str:
        with self.assertLogs("app.process_hardening", level="WARNING") as logs:
            self.assertIsNone(self.use())
        self.assertEqual(tempfile.gettempdir(), self.system_temp)
        self.assertIsNone(self.process_hardening._private_temp)
        self.assertEqual(_sqlite_temp_directory(), self.sqlite_temp, "SQLite keeps the temp directory it had")
        (line,) = logs.output
        self.assertIn("system temp directory", line)
        return line

    def test_a_root_that_is_not_there_keeps_the_system_temp_directory_with_one_warning(self) -> None:
        self.home.rmdir()
        self.root.rmdir()

        self.assertIn("is not a directory", self._refused())

    def test_a_link_in_the_directorys_place_is_refused_and_left_as_it_is(self) -> None:
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        self.private.symlink_to(elsewhere, target_is_directory=True)

        self.assertIn("is a link", self._refused())
        self.assertTrue(self.private.is_symlink() and elsewhere.is_dir())

    def test_a_file_in_the_directorys_place_is_refused_and_left_as_it_is(self) -> None:
        # B5-fix2 item 12 (H-8).
        self.private.write_bytes(b"not a directory")

        self.assertIn("is not a directory", self._refused())
        self.assertEqual(self.private.read_bytes(), b"not a directory")

    def test_a_directory_whose_mode_is_not_0700_is_refused(self) -> None:
        # B5-fix2 item 12 (H-9): 0600 opens nothing to others, and its owner cannot search it.
        for mode in (0o755, 0o600):
            with self.subTest(mode=oct(mode)):
                self.private.mkdir()
                self.private.chmod(mode)

                self.assertIn("mode %04o" % mode, self._refused())
                self.private.rmdir()

    def test_the_directory_is_0700_under_a_umask_that_takes_the_owners_bits_off(self) -> None:
        # B5-fix2 item 12 (H-12): the mkdir's mode is what the umask leaves; the chmod after it makes it 0700.
        umask = os.umask(0o277)
        try:
            made = self.use()
        finally:
            os.umask(umask)

        self.assertEqual(made, str(self.private))
        self.assertEqual(oct(os.lstat(self.private).st_mode & 0o7777), oct(0o700))

    def test_a_directory_not_this_users_is_refused(self) -> None:
        self.private.mkdir(mode=0o700)
        uid = os.geteuid()

        with patch.object(self.process_hardening.os, "geteuid", return_value=uid + 1):
            self.assertIn(f"owned by uid {uid}", self._refused())

    def test_a_leftover_that_cannot_be_removed_is_refused_and_the_warning_names_the_directory_and_its_whole_path(
        self,
    ) -> None:
        # B5-fix2 item 9 (OC2-7): a directory of mode 0500 holding a file, which its owner cannot empty (measured as uid
        # 10002), stood here as an unlink that is refused, so that root's run fails it too. rmtree's error names only
        # the last part ("[Errno 13] Permission denied: 'f'").
        (self.private / "ro" / "deep").mkdir(parents=True)
        self.private.chmod(0o700)
        (self.private / "ro" / "deep" / "f").write_bytes(b"left")
        unlink = os.unlink

        def refused(name, *args, **kwargs):
            if os.path.basename(name) == "f":
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), name)
            return unlink(name, *args, **kwargs)

        with patch.object(os, "unlink", refused):
            line = self._refused()

        self.assertIn(f"what an earlier start left in {str(self.private)!r} could not be removed", line)
        self.assertIn(repr(str(self.private / "ro" / "deep" / "f")), line)
        # B5-fix2 item 12 (H-13): once the leftover can be removed, the next start empties the directory and keeps it.
        self.assertEqual(self.use(), str(self.private))
        self.assertEqual(list(self.private.iterdir()), [])

    def test_an_error_hook_called_again_for_the_parent_names_the_path_that_failed_first(self) -> None:
        # B5-fix3 item 7 (C3-8): Python 3.12's rmtree, given a hook that raises, calls it again for the directory
        # above (Codex ran 3.12.10's shutil): the last path the hook saw was that directory, not the file it could not
        # remove. A stand-in rmtree calls it child, then parent, as 3.12 does.
        (self.private / "ro").mkdir(parents=True)
        self.private.chmod(0o700)
        child, parent = str(self.private / "ro" / "deep" / "f"), str(self.private / "ro" / "deep")

        def rmtree(path: str, **hooks) -> None:
            ((_, hook),) = hooks.items()
            try:
                try:
                    raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), "f")
                except OSError as error:
                    hook(os.unlink, child, error)
            except OSError as error:
                hook(os.rmdir, parent, error)

        with patch.object(self.process_hardening.shutil, "rmtree", rmtree):
            line = self._refused()

        self.assertIn(f"could not be removed: {child!r}, ", line)

    def test_a_path_that_failed_past_200_characters_is_named_whole(self) -> None:
        # B5-fix3 item 7 (C3-9): quoted_for_log cut it to 200 characters, where _emptied's docstring says whole.
        deep = self.private / ("p" * 120) / ("q" * 120)
        deep.mkdir(parents=True)
        self.private.chmod(0o700)
        (deep / "f").write_bytes(b"left")
        unlink = os.unlink

        def refused(name, *args, **kwargs):
            if os.path.basename(name) == "f":
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), name)
            return unlink(name, *args, **kwargs)

        with patch.object(os, "unlink", refused):
            line = self._refused()

        self.assertIn(repr(str(deep / "f")) + ", ", line)


class PrivateTempOffLinuxTests(_PrivateTempCase):
    @unittest.skipIf(sys.platform.startswith("linux"), "this is what other platforms do")
    def test_off_linux_nothing_is_made_and_the_system_temp_directory_stays(self) -> None:
        self.assertIsNone(self.use())
        self.assertFalse(self.private.exists())
        self.assertEqual(tempfile.gettempdir(), self.system_temp)


class ChildEnvironmentTests(unittest.TestCase):
    def test_every_child_but_a_provider_cli_and_the_regex_worker_gets_its_environment_from_child_environment(
        self,
    ) -> None:
        # B5-fix2 item 5 (S2-13): child_environment is the one name for the environment of a child of the
        # controller's own, with its temp directory. A provider CLI's (providers/base.py, and the launcher's probe,
        # started as a CLI is) keeps the operator's TMPDIR, and the regex worker, which writes nothing, has none.
        others = []
        for path in sorted(_APP.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                for keyword in node.keywords if isinstance(node, ast.Call) else ():
                    value = keyword.value
                    called = getattr(getattr(value, "func", None), "id", None)
                    if keyword.arg == "env" and called != "child_environment":
                        others.append(path.relative_to(_APP).as_posix())

        # providers/base.py twice: the CLI's environment passed to launch_command, then to its spawn.
        self.assertEqual(
            sorted(others),
            ["cli_sandbox.py", "harness/verifier/programmatic.py", "providers/base.py", "providers/base.py"],
        )


@unittest.skipUnless(sys.platform.startswith("linux"), "the CLI sandbox, and so this directory, are Linux's")
class PrivateTempOnTheKernelTests(_PrivateTempCase):
    def test_ocr_as_the_controller_runs_it_makes_and_leaves_nothing_in_the_system_temp_directory(self) -> None:
        from PIL import Image

        from app import ocr

        if not ocr.tesseract_available():
            self.skipTest("no tesseract here")
        shot = self.root.parent / "shot.png"
        Image.new("RGB", (64, 32), "white").save(shot)
        self.use()
        seen: list[tuple[list[str], list[str]]] = []
        module = ocr.pytesseract.pytesseract
        real_popen = module.subprocess.Popen

        def watched(*args, **kwargs):
            started = real_popen(*args, **kwargs)
            seen.append((sorted(os.listdir(self.system_temp)), sorted(os.listdir(self.private))))
            return started

        before = sorted(os.listdir(self.system_temp))
        extractor = ocr.OCRExtractor(enabled=True, language="eng", max_blocks=10, text_limit=200)
        with patch.object(module.subprocess, "Popen", watched):
            result = asyncio.run(extractor.extract_from_image(shot))

        # B5-fix2 item 12 (H-14): Tesseract ran, and read the screenshot; an OCR that failed would leave this too.
        self.assertTrue(result["available"] and "error" not in result, result)
        while_tesseract_ran = seen[-1]
        self.assertEqual(while_tesseract_ran[0], before, "the system temp directory changed while tesseract ran")
        self.assertTrue([name for name in while_tesseract_ran[1] if name.startswith("tess_")], seen)
        self.assertEqual(sorted(os.listdir(self.system_temp)), before)
        self.assertEqual(os.listdir(self.private), [])

    def test_a_sort_and_a_temp_table_too_large_for_a_stores_cache_put_sqlites_temp_files_in_the_directory(self) -> None:
        # SQLite unlinks a temp file as it opens it, so each is read from /proc/self/fd while it is open: a sort's
        # while its statement is, a temp table's while its connection is.
        from app.sqlite_utils import connect_sqlite

        self.use()
        connection = connect_sqlite(self.root.parent / "store.db", row_factory=sqlite3.Row)  # as the stores connect
        self.addCleanup(connection.close)
        connection.execute("PRAGMA cache_size=10")
        connection.execute("CREATE TABLE kept (key TEXT)")
        connection.executemany("INSERT INTO kept VALUES (?)", ((os.urandom(16).hex(),) for _ in range(_SPILLED_ROWS)))
        connection.commit()
        before = _open_files()

        cursor = connection.execute("SELECT key FROM kept ORDER BY random()")
        cursor.fetchone()
        while_sorting = _open_files() - before
        cursor.close()
        connection.execute("CREATE TEMP TABLE copied AS SELECT key FROM kept")
        with_a_temp_table = _open_files() - before

        for made in (while_sorting, with_a_temp_table):
            self.assertTrue(made, "SQLite made no temp file")
            self.assertEqual({os.path.dirname(path) for path in made}, {str(self.private)}, sorted(made))

    def test_a_cli_the_controller_starts_under_the_launcher_cannot_list_or_read_the_directory(self) -> None:
        # B5-fix2 item 12 (H-15): the command is launch_command's, as run_cli starts a CLI, so the grants are the
        # controller's own (the CLI home, the shared temp directories but /tmp, which holds this test's root, and /dev),
        # and a change to them shows here; it runs as the launcher tests' _stand_in runs its command.
        from app import cli_sandbox
        from app.config import Settings
        from tests.test_cli_sandbox_launcher import CHILD_ENV, LANDLOCK_ABI, STAND_IN, _run

        if LANDLOCK_ABI < 1:
            self.skipTest("needs Linux with Landlock")
        self.use()
        kept = self.private / "tess_kept_input.PNG"
        kept.write_bytes(b"screenshot")
        steps = [["list", str(self.private)], ["read", str(kept)], ["create", str(self.private / "planted")]]
        settings = Settings(_env_file=None, CLI_HOME=str(self.home))
        cli = [sys.executable, "-c", STAND_IN, json.dumps(steps)]

        command = asyncio.run(
            cli_sandbox.launch_command(settings, cli, cli_home=str(self.home), image_path=None, env=CHILD_ENV)
        )
        done = _run(command, SimpleNamespace(root=self.root, home=self.home))

        self.assertEqual(command[3], cli_sandbox.LAUNCHER, "it runs under the launcher")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(json.loads(done.stdout)["results"], ["EACCES", "EACCES", "EACCES"])
        self.assertFalse((self.private / "planted").exists())


if __name__ == "__main__":
    unittest.main()
