from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import utils as utils_module
from app.mesh import identity as identity_module
from app.mesh.identity import NodeIdentity

_POSIX_ONLY = unittest.skipUnless(os.name == "posix", "POSIX file modes")
_PRIVATE_DIR = 0o700


class NodeIdentityKeyFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "mesh" / "identity"

    def test_private_key_is_created_with_mode_0600_and_exclusive(self):
        real_open = os.open
        calls = []

        def spy(path, flags, mode=0o777, **kwargs):
            calls.append((Path(path).name, flags, mode))
            return real_open(path, flags, mode, **kwargs)

        with patch.object(identity_module.os, "open", spy):
            NodeIdentity(self.dir)

        private = [c for c in calls if c[0] == "node_private.pem"]
        self.assertEqual(len(private), 1)
        _, flags, mode = private[0]
        self.assertEqual(mode, 0o600)
        self.assertTrue(flags & os.O_EXCL)
        self.assertTrue(flags & os.O_CREAT)

    def test_identity_directory_is_requested_at_0700(self):
        modes = []
        real_mkdir = Path.mkdir

        def spy(self_path, mode=0o777, parents=False, exist_ok=False):
            modes.append((self_path, mode))
            return real_mkdir(self_path, mode=mode, parents=parents, exist_ok=exist_ok)

        with patch.object(Path, "mkdir", spy):
            NodeIdentity(self.dir)

        self.assertIn((self.dir, 0o700), modes)

    def test_the_key_survives_a_reload_with_the_same_node_id(self):
        first = NodeIdentity(self.dir).node_id
        self.assertEqual(NodeIdentity(self.dir).node_id, first)

    def test_a_key_another_start_created_after_the_check_is_loaded(self):
        # R5C-8: the file appears between exists() and the exclusive create, and the node keeps that key.
        first = NodeIdentity(self.dir).node_id
        real_exists = Path.exists

        def not_yet(path, *args, **kwargs):
            return False if path.name == "node_private.pem" else real_exists(path, *args, **kwargs)

        with patch.object(Path, "exists", not_yet):
            self.assertEqual(NodeIdentity(self.dir).node_id, first)

    @_POSIX_ONLY
    def test_a_loose_key_found_at_the_exclusive_create_is_tightened_as_any_loaded_key(self):
        NodeIdentity(self.dir)
        key = self.dir / "node_private.pem"
        key.chmod(0o644)
        real_exists = Path.exists

        def not_yet(path, *args, **kwargs):
            return False if path.name == "node_private.pem" else real_exists(path, *args, **kwargs)

        with patch.object(Path, "exists", not_yet):
            NodeIdentity(self.dir)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

    @_POSIX_ONLY
    def test_new_key_file_and_directory_have_private_modes(self):
        NodeIdentity(self.dir)
        self.assertEqual(stat.S_IMODE((self.dir / "node_private.pem").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)


class NodeIdentityKeyBeingWrittenTests(unittest.TestCase):
    """A start can read another start's key file before its bytes are in: _write_private_file creates, then writes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "mesh" / "identity"
        self.node_id = NodeIdentity(self.dir).node_id
        self.key = self.dir / "node_private.pem"
        self.pem = self.key.read_bytes()
        self.reads = 0

    def reading_the_key_as(self, *contents):
        """The key file reads as each of these in turn, the last for good; every other file as itself."""
        real_read_bytes = Path.read_bytes
        remaining = iter(contents)
        last = contents[-1]

        def read_bytes(path):
            if path != self.key:
                return real_read_bytes(path)
            self.reads += 1
            return next(remaining, last)

        return patch.object(Path, "read_bytes", read_bytes)

    def with_a_short_bound(self):
        return (
            patch.object(identity_module, "_FILE_BEING_WRITTEN_WAIT_SECONDS", 0.05),
            patch.object(identity_module, "_FILE_BEING_WRITTEN_POLL_SECONDS", 0.01),
        )

    def test_a_key_that_is_empty_at_first_and_complete_on_a_later_read_is_loaded(self):
        with self.reading_the_key_as(b"", self.pem):
            self.assertEqual(NodeIdentity(self.dir).node_id, self.node_id)
        self.assertEqual(self.reads, 2)

    def test_a_key_that_is_partly_written_at_first_is_loaded_once_whole(self):
        half = self.pem[: len(self.pem) // 2]
        with self.reading_the_key_as(half, half, self.pem):
            self.assertEqual(NodeIdentity(self.dir).node_id, self.node_id)
        self.assertEqual(self.reads, 3)

    def test_the_loser_at_the_exclusive_create_waits_for_the_winners_bytes(self):
        # The file appears between exists() and the exclusive create, and it is still empty.
        real_exists = Path.exists

        def not_yet(path, *args, **kwargs):
            return False if path.name == "node_private.pem" else real_exists(path, *args, **kwargs)

        with patch.object(Path, "exists", not_yet), self.reading_the_key_as(b"", self.pem):
            self.assertEqual(NodeIdentity(self.dir).node_id, self.node_id)
        self.assertEqual(self.reads, 2)

    def test_a_key_that_stays_empty_raises_by_the_bound(self):
        # Long enough that a slow runner still reads twice before the bound.
        wait = patch.object(identity_module, "_FILE_BEING_WRITTEN_WAIT_SECONDS", 0.5)
        poll = patch.object(identity_module, "_FILE_BEING_WRITTEN_POLL_SECONDS", 0.01)
        started = time.monotonic()
        with wait, poll, self.reading_the_key_as(b""), self.assertRaises(ValueError):
            NodeIdentity(self.dir)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertGreater(self.reads, 1)

    def test_a_key_that_is_gone_when_read_is_retried_within_the_bound_too(self):
        wait, poll = self.with_a_short_bound()

        def gone(path):
            raise FileNotFoundError(path)

        with wait, poll, patch.object(Path, "read_bytes", gone), self.assertRaises(FileNotFoundError):
            NodeIdentity(self.dir)


class NodeIdentityPublishedFilesTests(unittest.TestCase):
    """The public key and node_meta.json are published whole: a reader during the write sees all or nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "mesh" / "identity"

    def test_each_file_is_put_in_place_only_once_it_is_complete(self):
        real_replace = utils_module.replace_retrying
        seen: dict[str, tuple[bool, str]] = {}

        def probe(source, destination):
            destination = Path(destination)
            if destination.name in ("node_public.pem", "node_meta.json"):
                seen[destination.name] = (destination.exists(), Path(source).read_text(encoding="utf-8"))
            real_replace(source, destination)

        with patch.object(utils_module, "replace_retrying", probe):
            identity = NodeIdentity(self.dir)

        self.assertEqual(set(seen), {"node_public.pem", "node_meta.json"})
        self.assertFalse(seen["node_public.pem"][0], "the public key was already at its name before it was complete")
        self.assertFalse(seen["node_meta.json"][0], "node_meta.json was already at its name before it was complete")
        self.assertIn("-----BEGIN PUBLIC KEY-----", seen["node_public.pem"][1])
        self.assertIn("-----END PUBLIC KEY-----", seen["node_public.pem"][1])
        self.assertEqual(json.loads(seen["node_meta.json"][1])["node_id"], identity.node_id)

    def test_a_meta_read_refused_for_a_moment_is_retried(self):
        # On Windows a reader that opens the file while another start's replace lands is refused for an instant.
        NodeIdentity(self.dir)
        meta = self.dir / "node_meta.json"
        real_read_text = Path.read_text
        refusals = iter([PermissionError(13, "sharing violation")])

        def read_text(path, *args, **kwargs):
            if path == meta:
                refusal = next(refusals, None)
                if refusal is not None:
                    raise refusal
            return real_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            identity = NodeIdentity(self.dir)

        self.assertEqual(identity.node_id, json.loads(meta.read_text())["node_id"])

    def test_a_meta_read_that_keeps_failing_raises_by_the_bound(self):
        NodeIdentity(self.dir)
        meta = self.dir / "node_meta.json"
        real_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == meta:
                raise PermissionError(13, "sharing violation")
            return real_read_text(path, *args, **kwargs)

        with (
            patch.object(identity_module, "_FILE_BEING_WRITTEN_WAIT_SECONDS", 0.05),
            patch.object(identity_module, "_FILE_BEING_WRITTEN_POLL_SECONDS", 0.01),
            patch.object(Path, "read_text", read_text),
            self.assertRaises(PermissionError),
        ):
            NodeIdentity(self.dir)

    def test_files_already_there_are_left_as_they_are(self):
        NodeIdentity(self.dir)
        meta = self.dir / "node_meta.json"
        before = meta.read_text(encoding="utf-8")
        probe = Mock(side_effect=AssertionError("published again"))

        with patch.object(utils_module, "replace_retrying", probe):
            NodeIdentity(self.dir)

        self.assertEqual(meta.read_text(encoding="utf-8"), before)

    @_POSIX_ONLY
    def test_the_published_files_keep_the_mode_an_ordinary_write_gave_them(self):
        NodeIdentity(self.dir)
        umask = os.umask(0)
        os.umask(umask)
        for name in ("node_public.pem", "node_meta.json"):
            self.assertEqual(stat.S_IMODE((self.dir / name).stat().st_mode), 0o666 & ~umask, name)


class NodeIdentityLooseKeyWarningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "identity"
        self.node_id = NodeIdentity(self.dir).node_id
        self.key = self.dir / identity_module._PRIVKEY_FILE

    def _reload(self, **patches):
        with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
            with patch.multiple(identity_module.os, **patches) if patches else _nothing():
                loaded = NodeIdentity(self.dir)
        self.assertEqual(loaded.node_id, self.node_id)
        return "\n".join(logs.output)

    @_POSIX_ONLY
    def test_w4_15_a_group_or_world_readable_key_is_tightened_to_0600_with_a_warning(self):
        for mode in (0o640, 0o644, 0o604, 0o666):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.key, mode)
                output = self._reload()
                self.assertIn("readable by group or others", output)
                self.assertIn("changed it to 0600", output)
                self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)

    @_POSIX_ONLY
    def test_w4_15_a_key_that_cannot_be_tightened_is_still_loaded_with_a_warning(self):
        os.chmod(self.key, 0o644)
        output = self._reload(fchmod=Mock(side_effect=PermissionError(1, "Operation not permitted")))
        self.assertIn("could not be tightened", output)
        self.assertIn("stays readable", output)
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o644)

    def test_private_key_does_not_warn(self):
        with self.assertNoLogs("app.mesh.identity", level="WARNING"):
            NodeIdentity(self.dir)


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class NodeIdentityLooseDirectoryTests(unittest.TestCase):
    """ACT-39/HARN-29: mkdir's 0700 applies only to a new directory, so one an older release made at 0755 kept it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "identity"
        NodeIdentity(self.dir)

    def _load(self, dir_mode: int, *, ours: bool, chmod=None):
        """Load with the directory's mode and owner injected (the key reads as 0600); returns the chmod mock.

        The default chmod sets the injected mode, as a filesystem that keeps modes does. The key's own check is
        replaced (W10-11): it reads os.fstat on the open key, which on Windows reports 0666 and goes on to os.fchmod
        (absent there before Python 3.13); the key's tightening keeps its own POSIX-only tests.
        """
        modes = {"dir": dir_mode}

        def keep_mode(path, mode):
            modes["dir"] = mode

        with (
            patch.object(identity_module, "_POSIX", True),
            patch.object(identity_module, "_tighten_a_loose_key", Mock()),
            patch.object(identity_module, "_mode_bits", side_effect=lambda p: modes["dir"] if p == self.dir else 0o600),
            patch.object(identity_module, "_owned_by_this_process", return_value=ours),
            patch.object(identity_module.os, "chmod", chmod or Mock(side_effect=keep_mode)) as chmod_mock,
        ):
            NodeIdentity(self.dir)
        return chmod_mock

    def test_a_loose_directory_of_ours_is_tightened_and_logged_once(self):
        for mode in (0o755, 0o750, 0o705):
            with self.subTest(mode=oct(mode)):
                with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
                    chmod = self._load(mode, ours=True)

                chmod.assert_called_once_with(self.dir, 0o700)
                warnings = [line for line in logs.output if "identity directory" in line]
                self.assertEqual(len(warnings), 1)
                self.assertIn(oct(mode)[2:], warnings[0])

    def test_a_loose_directory_of_another_user_is_reported_and_left_alone(self):
        with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
            chmod = self._load(0o755, ours=False)

        chmod.assert_not_called()
        self.assertTrue(any("belongs to another user" in line for line in logs.output))

    def test_a_directory_that_cannot_be_tightened_is_reported(self):
        with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
            self._load(0o755, ours=True, chmod=Mock(side_effect=PermissionError(1, "Operation not permitted")))

        self.assertTrue(any("could not be tightened (PermissionError)" in line for line in logs.output))

    def test_a_chmod_the_filesystem_does_not_keep_is_reported_not_called_tightened(self):
        # A mount that fakes its modes (a Windows folder through Docker Desktop shows 0777) takes the chmod and keeps
        # showing the old mode: "tightened" would be logged on every start, and be false.
        with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
            chmod = self._load(0o777, ours=True, chmod=Mock())

        chmod.assert_called_once_with(self.dir, 0o700)
        text = "\n".join(logs.output)
        self.assertNotIn("tightened it to 700", text)
        self.assertIn("did not change", text)

    @_POSIX_ONLY
    def test_a_loose_directory_is_really_tightened(self):
        # Nothing injected: the real owner check and the real chmod (the test owns its temp directory, as root too).
        os.chmod(self.dir, 0o740)  # readable by the group: loose

        with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
            NodeIdentity(self.dir)

        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len([line for line in logs.output if "identity directory" in line]), 1)

    def test_a_add_10_a_loose_directory_holding_other_files_keeps_its_mode_and_is_reported(self):
        # MESH_IDENTITY_DIR pointed at /data/mesh or /data: a chmod 700 there locks every other reader out.
        for other in ("peers.json", "artifacts"):
            with self.subTest(other=other):
                path = self.dir / other
                path.mkdir() if "." not in other else path.write_text("{}", encoding="utf-8")
                with self.assertLogs("app.mesh.identity", level="WARNING") as logs:
                    chmod = self._load(0o755, ours=True)

                chmod.assert_not_called()
                text = "\n".join(logs.output)
                self.assertIn("holds other files", text)
                self.assertNotIn("tightened it to 700", text)
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_a_add_10_the_witness_signer_tightens_only_a_directory_of_identity_files(self):
        from app.witness_signing import WitnessSigner

        def load(dir_mode: int):
            modes = {"dir": dir_mode}
            with (
                patch.object(identity_module, "_POSIX", True),
                patch.object(identity_module, "_tighten_a_loose_key", Mock()),  # W10-11, as in _load
                patch.object(
                    identity_module, "_mode_bits", side_effect=lambda p: modes["dir"] if p == self.dir else 0o600
                ),
                patch.object(identity_module, "_owned_by_this_process", return_value=True),
                patch.object(identity_module.os, "chmod", Mock()) as chmod_mock,
            ):
                WitnessSigner(self.dir)
            return chmod_mock

        load(0o755).assert_called_once_with(self.dir, 0o700)
        (self.dir / "receipts.jsonl").write_text("", encoding="utf-8")
        with self.assertLogs("app.mesh.identity", level="WARNING"):
            load(0o755).assert_not_called()

    def test_a_private_directory_is_left_alone(self):
        with self.assertNoLogs("app.mesh.identity", level="WARNING"):
            chmod = self._load(_PRIVATE_DIR, ours=True)

        chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()
