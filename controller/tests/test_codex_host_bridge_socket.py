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
        # prepare_socket_path keeps its lock for the bridge's life; a test gives back the ones it took.
        self.addCleanup(lambda: [os.close(fd) for fd in getattr(bridge, "_SOCKET_LOCKS", {}).values()])
        self.addCleanup(lambda: getattr(bridge, "_SOCKET_LOCKS", {}).clear())

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

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "needs AF_UNIX")
    def test_w4_8_a_socket_another_bridge_is_listening_on_is_refused_and_kept(self) -> None:
        # Any socket was removed, a live one included: a second bridge took over the first one's path.
        target = self.root / "codex.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(target))
        listener.listen(1)
        with self.assertRaises(bridge.SocketPathError) as caught:
            bridge.prepare_socket_path(str(target))
        self.assertIn("another bridge is listening", str(caught.exception))
        self.assertIn(str(target.resolve()), str(caught.exception))
        self.assertTrue(target.exists())
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.connect(str(target))  # still the first listener's

    @unittest.skipUnless(os.name == "posix", "needs symlinks")
    def test_a_symlink_at_the_path_is_refused_and_its_target_kept(self) -> None:
        real = self.root / "real.txt"
        real.write_text("precious", encoding="utf-8")
        link = self.root / "codex.sock"
        link.symlink_to(real)
        with self.assertRaises(bridge.SocketPathError):
            bridge.prepare_socket_path(str(link))
        self.assertEqual(real.read_text(encoding="utf-8"), "precious")


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
@unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "flock and AF_UNIX are POSIX-only")
class BridgeSocketLockTests(unittest.TestCase):
    """W5-7: the probe-then-unlink raced a second bridge, and each bridge's shutdown removed whatever socket was there."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.addCleanup(lambda: [os.close(fd) for fd in getattr(bridge, "_SOCKET_LOCKS", {}).values()])
        self.addCleanup(lambda: getattr(bridge, "_SOCKET_LOCKS", {}).clear())

    def test_a_second_bridge_stops_while_the_first_holds_the_lock_and_the_socket_is_kept(self) -> None:
        import fcntl

        target = self.root / "codex.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(target))
        stale.close()  # refused, so the probe alone would call it stale and remove it
        lock_path = Path(str(target.resolve()) + ".lock")
        holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with self.assertRaises(bridge.SocketPathError) as caught:
            bridge.prepare_socket_path(str(target))
        self.assertIn(f"another bridge is running on {target.resolve()}", str(caught.exception))
        self.assertTrue(target.exists())

    def test_the_lock_is_taken_beside_the_socket_at_0600_and_kept(self) -> None:
        target = self.root / "codex.sock"
        path = bridge.prepare_socket_path(str(target))
        lock_path = Path(str(path) + ".lock")
        self.assertEqual(os.stat(lock_path).st_mode & 0o777, 0o600)
        with self.assertRaises(bridge.SocketPathError):
            bridge.prepare_socket_path(str(target))  # another open of the same lock: refused while it is held

    def test_a_refused_start_gives_its_lock_back(self) -> None:
        target = self.root / "codex.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(target))
        listener.listen(1)
        for _ in range(2):  # the second start is refused by the probe again, not by a lock the first one kept
            with self.assertRaises(bridge.SocketPathError) as caught:
                bridge.prepare_socket_path(str(target))
            self.assertIn("listening", str(caught.exception))

    def test_shutdown_leaves_a_socket_that_replaced_the_one_it_bound(self) -> None:
        import contextlib
        import io
        from types import SimpleNamespace

        socket_path = self.root / "codex.sock"
        replacement = self.root / "replacement"

        class _StoppedServer:
            def __init__(self, path, service) -> None:
                Path(path).touch()

            def serve_forever(self) -> None:
                replacement.write_text("another bridge's", encoding="utf-8")
                os.replace(replacement, socket_path)  # another bridge took the path while this one served

            def server_close(self) -> None:
                pass

        args = SimpleNamespace(
            socket_path=str(socket_path), codex_path="codex", model=None, request_timeout_seconds=55.0, pass_env=[]
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(bridge, "parse_args", return_value=args),
            patch.object(bridge, "prepare_socket_path", return_value=socket_path),
            patch.object(bridge, "UnixHTTPServer", _StoppedServer),
            patch.object(bridge.signal, "signal"),
            patch.object(bridge.os, "umask"),
            patch.object(bridge.os, "chmod"),
            patch.object(bridge, "make_non_dumpable"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(bridge.main(), 0)

        self.assertEqual(socket_path.read_text(encoding="utf-8"), "another bridge's")

    def test_shutdown_removes_the_socket_it_bound(self) -> None:
        import contextlib
        import io
        from types import SimpleNamespace

        socket_path = self.root / "codex.sock"

        class _StoppedServer:
            def __init__(self, path, service) -> None:
                Path(path).touch()

            def serve_forever(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        args = SimpleNamespace(
            socket_path=str(socket_path), codex_path="codex", model=None, request_timeout_seconds=55.0, pass_env=[]
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(bridge, "parse_args", return_value=args),
            patch.object(bridge, "prepare_socket_path", return_value=socket_path),
            patch.object(bridge, "UnixHTTPServer", _StoppedServer),
            patch.object(bridge.signal, "signal"),
            patch.object(bridge.os, "umask"),
            patch.object(bridge.os, "chmod"),
            patch.object(bridge, "make_non_dumpable"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(bridge.main(), 0)

        self.assertFalse(socket_path.exists())


if __name__ == "__main__":
    unittest.main()
