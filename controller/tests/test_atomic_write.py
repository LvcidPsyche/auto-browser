from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils import atomic_write_text


class AtomicWriteTextTests(unittest.TestCase):
    def test_replaces_content_and_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "record.json"
            atomic_write_text(path, "first")
            atomic_write_text(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "second")
            self.assertEqual(os.listdir(tmpdir), ["record.json"])

    def test_failed_rename_keeps_old_content_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "record.json"
            atomic_write_text(path, "old")
            with patch("app.utils.os.replace", side_effect=OSError("disk gone")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(os.listdir(tmpdir), ["record.json"])

    @unittest.skipIf(os.name != "posix", "POSIX permissions")
    def test_new_files_get_umask_permissions_not_mkstemp_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "record.json"
            atomic_write_text(path, "x")
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(path.stat().st_mode & 0o777, 0o666 & ~umask)
