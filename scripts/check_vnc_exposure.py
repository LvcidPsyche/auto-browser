#!/usr/bin/env python3
"""Check that a running browser-node serves VNC only through noVNC.

    python3 scripts/check_vnc_exposure.py CONTAINER NOVNC_PORT [--expect-password]

Inside the container (via `docker exec -u browser`), it fails when anything listens
on a TCP port in the RFB range 5900-5999, when x11vnc holds any TCP or UDP socket,
or when x11vnc's unix socket is missing or sits in a directory other users can
enter. From this host it then speaks RFB through noVNC on 127.0.0.1:NOVNC_PORT with
noVNC's own Origin, which proves websockify reaches x11vnc over the socket, and
checks that a foreign Origin is refused. A node without VNC_PASSWORD must offer
security type None and reach ServerInit. With --expect-password, None must be
absent and VNC authentication offered; the password itself is not tried.

Standard library only: the inside half runs on the image's own python3.
"""

from __future__ import annotations

import argparse
import base64
import os
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path

PROC = "/proc"
RFB_PORTS = range(5900, 6000)
TCP_TABLES = ("net/tcp", "net/tcp6")
INET_TABLES = TCP_TABLES + ("net/udp", "net/udp6")
TCP_LISTEN = "0A"
X11VNC_LOG = "/tmp/x11vnc.log"
TIMEOUT_SECONDS = 10
SECURITY_NONE, SECURITY_VNC_AUTH = 1, 2  # RFB security types


def _table(name: str):
    """(local port, state, inode) for each socket in a /proc/net table."""
    with open(f"{PROC}/{name}", encoding="ascii") as table:
        next(table)
        for line in table:
            fields = line.split()
            yield int(fields[1].rsplit(":", 1)[1], 16), fields[3], fields[9]


def _socket_inodes(pid: str) -> set[str]:
    inodes = set()
    for fd in os.listdir(f"{PROC}/{pid}/fd"):
        try:
            target = os.readlink(f"{PROC}/{pid}/fd/{fd}")
        except FileNotFoundError:  # closed between listdir and readlink
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:[") : -1])
    return inodes


def _socket_owners() -> tuple[dict[str, str], list[str]]:
    """Socket inode -> "comm[pid]" for every process this user can inspect, and x11vnc's pids."""
    owners: dict[str, str] = {}
    x11vnc: list[str] = []
    for pid in filter(str.isdigit, os.listdir(PROC)):
        try:
            comm = Path(f"{PROC}/{pid}/comm").read_text(encoding="utf-8").strip()
            inodes = _socket_inodes(pid)
        except FileNotFoundError:  # the process exited
            continue
        except PermissionError:  # another user's process; the listener table says so
            continue
        owners.update(dict.fromkeys(inodes, f"{comm}[{pid}]"))
        if comm == "x11vnc":
            x11vnc.append(pid)
    return owners, x11vnc


def _x11vnc_log_tail() -> str:
    try:
        return "".join(Path(X11VNC_LOG).read_text(encoding="utf-8", errors="replace").splitlines(True)[-20:])
    except OSError as exc:
        return f"(could not read {X11VNC_LOG}: {exc})"


def _listener_failures(owners: dict[str, str]) -> list[str]:
    failures = []
    print("TCP listeners in the container:")
    for name in TCP_TABLES:
        for port, state, inode in _table(name):
            if state != TCP_LISTEN:
                continue
            print(f"  {name:<8} port {port:<5} {owners.get(inode, '(owner not readable as this user)')}")
            if port in RFB_PORTS:
                failures.append(f"something listens on TCP port {port}, an RFB port ({name})")
    held = [
        (name, port)
        for name in INET_TABLES
        for port, _, inode in _table(name)
        if owners.get(inode, "").startswith("x11vnc[")
    ]
    if held:
        failures.append(f"x11vnc holds inet sockets (table, local port): {held}")
    return failures


def _unix_socket_failures(pid: str) -> list[str]:
    args = Path(f"{PROC}/{pid}/cmdline").read_bytes().decode("utf-8").split("\0")
    print("x11vnc:", " ".join(args))
    if "-unixsock" not in args:
        return ["x11vnc runs without -unixsock"]
    sock_path = Path(args[args.index("-unixsock") + 1])
    uid = int(Path(f"{PROC}/{pid}/status").read_text(encoding="utf-8").split("\nUid:")[1].split()[0])
    try:
        sock_mode = sock_path.stat().st_mode
        parent = sock_path.parent.stat()
    except FileNotFoundError as exc:
        return [f"x11vnc's socket is missing: {exc}\n{_x11vnc_log_tail()}"]
    failures = []
    if not stat.S_ISSOCK(sock_mode):
        failures.append(f"{sock_path} is not a socket")
    if stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != uid:
        failures.append(
            f"{sock_path.parent} is mode {stat.S_IMODE(parent.st_mode):o} owned by uid {parent.st_uid}; "
            f"want 700 owned by x11vnc's uid {uid}"
        )
    return failures


def inside() -> int:
    owners, x11vnc = _socket_owners()
    if len(x11vnc) != 1:
        print(f"FAIL: expected one x11vnc process this user can inspect, found {len(x11vnc)}\n{_x11vnc_log_tail()}")
        return 1
    failures = _listener_failures(owners) + _unix_socket_failures(x11vnc[0])
    for failure in failures:
        print("FAIL:", failure)
    if not failures:
        print("ok: no RFB TCP listener; x11vnc serves only its unix socket, in a 0700 directory")
    return 1 if failures else 0


class WebSocketStream:
    """Just enough of RFC 6455 to speak RFB through websockify: binary frames, client side."""

    def __init__(self, sock: socket.socket, pending: bytes) -> None:
        self.sock, self.raw, self.data = sock, pending, b""

    def _raw(self, size: int) -> bytes:
        while len(self.raw) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("noVNC closed the connection")
            self.raw += chunk
        out, self.raw = self.raw[:size], self.raw[size:]
        return out

    def read(self, size: int) -> bytes:
        while len(self.data) < size:
            first, second = self._raw(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._raw(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._raw(8))[0]
            payload = self._raw(length)
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError(f"noVNC closed the WebSocket: {payload[2:]!r}")
            if opcode in (0x0, 0x2):
                self.data += payload
        out, self.data = self.data[:size], self.data[size:]
        return out

    def write(self, data: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes([0x82, 0x80 | len(data)]) + mask + masked)


def _upgrade(host: str, port: int, origin: str) -> tuple[socket.socket, str, bytes]:
    sock = socket.create_connection((host, port), timeout=TIMEOUT_SECONDS)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.sendall(
        (
            f"GET /websockify HTTP/1.1\r\nHost: {host}:{port}\r\nOrigin: {origin}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: binary\r\n\r\n"
        ).encode("ascii")
    )
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    head, _, pending = response.partition(b"\r\n\r\n")
    return sock, head.split(b"\r\n", 1)[0].decode("latin-1"), pending


def _server_init(stream: WebSocketStream) -> str:
    """Choose security type None and read ServerInit; return the display it describes."""
    stream.write(bytes([SECURITY_NONE]))
    if struct.unpack(">I", stream.read(4))[0] != 0:
        raise RuntimeError("x11vnc rejected security type None")
    stream.write(b"\x01")  # ClientInit: shared
    width, height = struct.unpack(">HH", stream.read(4))
    stream.read(16)  # pixel format
    name = stream.read(struct.unpack(">I", stream.read(4))[0]).decode("utf-8", "replace")
    return f"display {width}x{height} {name!r}"


def takeover(host: str, port: int, *, expect_password: bool = False) -> str:
    """Speak RFB through noVNC as far as proves the node's VNC setup; return what it said.

    Without a password expected, security type None must be offered and the handshake must
    reach ServerInit. With one expected, None must be absent and VNC authentication offered;
    the password itself is not tried (the standard library has no DES).
    """
    sock, status, pending = _upgrade(host, port, f"http://{host}:{port}")
    with sock:
        if " 101 " not in f"{status} ":
            raise RuntimeError(f"noVNC refused its own origin: {status!r}")
        stream = WebSocketStream(sock, pending)
        version = stream.read(12)
        if not version.startswith(b"RFB 003."):
            raise RuntimeError(f"no RFB banner through noVNC: {version!r}")
        stream.write(b"RFB 003.008\n")
        security_types = list(stream.read(stream.read(1)[0]))
        if not security_types:
            raise RuntimeError(f"x11vnc refused the client: {stream.read(struct.unpack('>I', stream.read(4))[0])!r}")
        banner = version.decode().strip()
        if expect_password:
            if SECURITY_NONE in security_types:
                raise RuntimeError(
                    f"x11vnc offers security type None although a password is expected: {security_types}"
                )
            if SECURITY_VNC_AUTH not in security_types:
                raise RuntimeError(f"x11vnc does not offer VNC authentication (type 2): {security_types}")
            return f"{banner}, security type None not offered, VNC authentication (type 2) offered; password not tried"
        if SECURITY_NONE not in security_types:
            raise RuntimeError(
                f"x11vnc does not offer security type None (offered {security_types}); "
                "pass --expect-password if the node runs with VNC_PASSWORD"
            )
        return f"{banner}, {_server_init(stream)}"


def main(argv: list[str]) -> int:
    if argv == ["--inside"]:
        return inside()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("container", help="browser-node container name or id")
    parser.add_argument("novnc_port", type=int, help="host port noVNC (container port 6080) is published on")
    parser.add_argument("--host", default="127.0.0.1", help="host address noVNC is published on")
    parser.add_argument(
        "--expect-password",
        action="store_true",
        help="the node runs with VNC_PASSWORD: require VNC authentication and no security type None "
        "(the password itself is not tried)",
    )
    args = parser.parse_args(argv)

    with open(__file__, "rb") as source:
        result = subprocess.run(
            ["docker", "exec", "-i", "-u", "browser", args.container, "python3", "-I", "-", "--inside"],
            stdin=source,
            check=False,
        )
    if result.returncode != 0:
        return 1
    try:
        said = takeover(args.host, args.novnc_port, expect_password=args.expect_password)
        print("ok: takeover through noVNC reaches x11vnc:", said)
        sock, status, _ = _upgrade(args.host, args.novnc_port, "https://attacker.example")
        sock.close()
    except (OSError, RuntimeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    if " 101 " in f"{status} ":
        print(f"FAIL: noVNC accepted a foreign Origin: {status!r}")
        return 1
    print(f"ok: noVNC refuses a foreign Origin: {status!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
