"""The bridge only unlinks a socket, and refuses a path an AF_UNIX socket cannot bind (HARN-47)."""

from __future__ import annotations

import importlib.util
import os
import socket
import socketserver
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_host_bridge.py"


def _load_bridge():
    shimmed = not hasattr(socketserver, "UnixStreamServer")
    if shimmed:
        socketserver.UnixStreamServer = socketserver.TCPServer
    try:
        spec = importlib.util.spec_from_file_location("codex_host_bridge_socket_under_test", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if shimmed:
            del socketserver.UnixStreamServer


bridge = _load_bridge() if _SCRIPT.is_file() else None


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class PrepareSocketPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_regular_file_at_the_path_is_refused_and_kept(self) -> None:
        target = self.root / "codex.sock"
        target.write_text("precious", encoding="utf-8")
        with self.assertRaises(bridge.SocketPathError):
            bridge.prepare_socket_path(str(target))
        self.assertEqual(target.read_text(encoding="utf-8"), "precious")

    def test_a_directory_at_the_path_is_refused(self) -> None:
        target = self.root / "codex.sock"
        target.mkdir()
        with self.assertRaises(bridge.SocketPathError):
            bridge.prepare_socket_path(str(target))
        self.assertTrue(target.is_dir())

    def test_a_missing_path_is_fine_and_its_directory_is_created(self) -> None:
        path = bridge.prepare_socket_path(str(self.root / "sub" / "codex.sock"))
        self.assertEqual(path, (self.root / "sub" / "codex.sock").resolve())
        self.assertTrue((self.root / "sub").is_dir())

    def test_a_path_longer_than_the_socket_limit_is_refused_with_its_length(self) -> None:
        deep = self.root / ("d" * 60) / ("e" * 60) / "codex.sock"
        with patch.object(bridge, "AF_UNIX_PATH_MAX", 100):
            with self.assertRaises(bridge.SocketPathError) as caught:
                bridge.prepare_socket_path(str(deep))
        self.assertIn("too long", str(caught.exception))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "needs AF_UNIX")
    def test_a_stale_socket_is_removed(self) -> None:
        target = self.root / "codex.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(target))
        listener.close()
        self.assertTrue(target.exists())
        bridge.prepare_socket_path(str(target))
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "posix", "needs symlinks")
    def test_a_symlink_at_the_path_is_refused_and_its_target_kept(self) -> None:
        real = self.root / "real.txt"
        real.write_text("precious", encoding="utf-8")
        link = self.root / "codex.sock"
        link.symlink_to(real)
        with self.assertRaises(bridge.SocketPathError):
            bridge.prepare_socket_path(str(link))
        self.assertEqual(real.read_text(encoding="utf-8"), "precious")


if __name__ == "__main__":
    unittest.main()
