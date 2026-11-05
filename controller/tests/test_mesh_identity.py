from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.mesh import identity as identity_module
from app.mesh.identity import NodeIdentity

_POSIX_ONLY = unittest.skipUnless(os.name == "posix", "POSIX file modes")


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

    @_POSIX_ONLY
    def test_new_key_file_and_directory_have_private_modes(self):
        NodeIdentity(self.dir)
        self.assertEqual(stat.S_IMODE((self.dir / "node_private.pem").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)


class NodeIdentityLooseKeyWarningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "identity"
        NodeIdentity(self.dir)

    def _reload(self, mode: int):
        # The mode is injected so this runs the same on Windows, where chmod cannot loosen a file.
        with (
            patch.object(identity_module, "_POSIX", True),
            patch.object(identity_module, "_mode_bits", return_value=mode),
            self.assertLogs("app.mesh.identity", level="WARNING") as logs,
        ):
            NodeIdentity(self.dir)
        return logs.output

    def test_group_or_world_readable_key_warns_on_load(self):
        for mode in (0o640, 0o644, 0o604, 0o666):
            with self.subTest(mode=oct(mode)):
                output = self._reload(mode)
                self.assertTrue(any("readable by group or others" in line for line in output))

    def test_private_key_does_not_warn(self):
        with (
            patch.object(identity_module, "_POSIX", True),
            patch.object(identity_module, "_mode_bits", return_value=0o600),
            self.assertNoLogs("app.mesh.identity", level="WARNING"),
        ):
            NodeIdentity(self.dir)


if __name__ == "__main__":
    unittest.main()
