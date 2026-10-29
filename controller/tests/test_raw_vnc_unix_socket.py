"""Raw VNC (RFB) has no TCP listener anywhere: noVNC on 6080 is the only way to the desktop.

x11vnc's libvncserver (0.9.15 in the browser-node image, python:3.11-slim on Debian
trixie) answers a WebSocket upgrade on its own RFB port and never compares the
Origin header. A TCP RFB port next to websockify was therefore a way around noVNC's
origin guard (browser-node/novnc_origin.py) for anything that could reach it: the
127.0.0.1 publish on the host (a browser on the host included), other containers on
the compose network, and pages the agent's Chromium loads from a loopback origin.
x11vnc now serves RFB only on a unix socket in a directory private to the browser
user, and websockify proxies to that socket.
"""

from __future__ import annotations

import re
import shlex
import tempfile
import unittest
from pathlib import Path

import pytest

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_NODE = REPO_ROOT / "browser-node"
ENTRYPOINT = BROWSER_NODE / "entrypoint.sh"
DOCKERFILE = BROWSER_NODE / "Dockerfile"
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))
RFB_PORT = 5900

# browser-node/ and the compose files are not shipped in the controller image.
needs_checkout = pytest.mark.skipif(not ENTRYPOINT.is_file(), reason="repo checkout not available")


def _entrypoint() -> str:
    """entrypoint.sh with backslash continuations joined into single lines."""
    return ENTRYPOINT.read_text(encoding="utf-8").replace("\\\n", " ")


def _launch_args(program: str) -> list[str]:
    """The arguments of the `run_as_browser ... <program>` line, up to its output redirect."""
    for line in _entrypoint().splitlines():
        if re.search(rf"^\s*run_as_browser\b.*\b{program}\b", line):
            return shlex.split(line.split(">", 1)[0])
    raise AssertionError(f"entrypoint.sh has no run_as_browser line that starts {program}")


def _option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _assignment(name: str) -> str:
    match = re.search(rf"^{name}=(\S+)$", _entrypoint(), flags=re.MULTILINE)
    assert match is not None, f"entrypoint.sh does not set {name}"
    return shlex.split(match.group(1))[0]


def _resolve(value: str) -> str:
    """Compose interpolation replaced by its default: `${VNC_PORT:-5900}` reads as `5900`."""
    return re.sub(r"\$\{\w+(?::?-([^}]*))?[^}]*\}", lambda match: match.group(1) or "", value)


def _container_port(spec: str) -> str:
    """The container side of a port mapping: `127.0.0.1:${PORT:-1}:5900/tcp` and `[::1]:1:5900` give `5900`."""
    spec = _resolve(spec.strip().strip("'\"")).split("/", 1)[0]
    return re.sub(r"\[[^\]]*\]", "", spec).rsplit(":", 1)[-1].strip()


def _covers(spec: str, port: int) -> bool:
    """Whether a container port or range (`5900`, `5900-5910`) includes `port`."""
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
    return match is not None and int(match[1]) <= port <= int(match[2] or match[1])


def _published_container_ports(text: str) -> list[str]:
    """The container port of every `ports:` entry in a compose file, without a YAML parser.

    PyYAML is not a declared dependency. This reads flow lists (`ports: ["5900:5900"]`), short
    syntax quoted either way or not at all, and long syntax (`- target: 5900`, keys in any order).
    """
    lines = [re.sub(r"(^|\s+)#.*$", "", line) for line in text.splitlines()]
    found = []
    for number, line in enumerate(lines):
        key = re.match(r"^(\s*)ports:\s*(?:!\S+\s*)?(.*)$", line)
        if key is None:
            continue
        if key[2].startswith("["):
            found += [_container_port(item) for item in key[2].strip("[] ").split(",") if item.strip()]
            continue
        for entry in lines[number + 1 :]:
            if not entry.strip():
                continue
            if len(entry) - len(entry.lstrip()) <= len(key[1]):
                break
            item = entry.strip().removeprefix("- ").strip()
            field = re.fullmatch(r"([A-Za-z_]\w*):(?:\s+(.*))?", item)
            if field is None:
                found.append(_container_port(item))
            elif field[1] == "target":
                found.append(_container_port(field[2] or ""))
    return found


@needs_checkout
def test_x11vnc_listens_on_no_tcp_port() -> None:
    x11vnc = _launch_args("x11vnc")
    assert _option(x11vnc, "-rfbport") == "0", f"x11vnc listens for RFB on TCP (IPv4): {x11vnc}"
    # libvncserver opens its own IPv6 listener on 5900 (main.c sets ipv6port=5900) whatever
    # -rfbport says, and x11vnc never touches it; only -rfbportv6 0 turns it off.
    assert _option(x11vnc, "-rfbportv6") == "0", f"x11vnc still listens for RFB on TCP6 5900: {x11vnc}"
    assert _option(x11vnc, "-unixsock") is not None, f"x11vnc serves RFB on no unix socket: {x11vnc}"


@needs_checkout
def test_websockify_proxies_to_the_x11vnc_socket() -> None:
    websockify = _launch_args("websockify")
    tcp_targets = [arg for arg in websockify if re.fullmatch(r"[\w.\-\[\]:]*:\d+", arg)]
    assert tcp_targets == [], f"websockify proxies to a TCP RFB target: {websockify}"
    target = _option(websockify, "--unix-target")
    assert target is not None, f"websockify has no --unix-target: {websockify}"
    assert target == _option(_launch_args("x11vnc"), "-unixsock"), "websockify and x11vnc use different sockets"


@needs_checkout
def test_the_vnc_socket_sits_in_a_directory_only_the_browser_user_can_enter() -> None:
    socket_path = _option(_launch_args("x11vnc"), "-unixsock")
    assert socket_path == "$VNC_SOCKET", f"x11vnc's socket is not $VNC_SOCKET: {socket_path}"
    assert _assignment("VNC_SOCKET") == "$VNC_SOCKET_DIR/rfb.sock"
    assert _assignment("VNC_SOCKET_DIR").startswith("/tmp/runtime/")
    text = _entrypoint()
    created = re.search(r'^run_as_browser install -d -m 0700 "\$VNC_SOCKET_DIR"$', text, flags=re.MULTILINE)
    assert created is not None, "the socket directory is not created 0700 by the browser user"
    assert created.start() < text.index("run_as_browser x11vnc"), "the socket directory is created after x11vnc starts"


@needs_checkout
def test_the_browser_node_image_exposes_no_raw_vnc_port() -> None:
    exposed = re.findall(r"^EXPOSE\s+(.+)$", DOCKERFILE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    ports = [_container_port(port) for port in " ".join(exposed).split()]
    assert "6080" in ports, f"Dockerfile EXPOSE moved; update this test: {exposed}"
    assert not any(_covers(port, RFB_PORT) for port in ports), f"browser-node/Dockerfile exposes raw VNC: {exposed}"


@needs_checkout
def test_no_compose_file_publishes_raw_vnc() -> None:
    assert COMPOSE_FILES, "no docker-compose*.yml found"
    published = {path.name: _published_container_ports(path.read_text(encoding="utf-8")) for path in COMPOSE_FILES}
    assert {"6080", "8000"} <= set(published["docker-compose.yml"]), (
        f"port parsing broke; update this test: {published}"
    )
    offenders = {name: ports for name, ports in published.items() if any(_covers(port, RFB_PORT) for port in ports)}
    assert offenders == {}, f"compose publishes raw VNC: {offenders}"


class IsolatedSessionPortsTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_isolated_session_publishes_only_novnc(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            settings = Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"))
            controller_container = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={
                    "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
                    "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
                },
            )
            client = FakeDockerClient(controller_container)
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)
            provisioner._controller_container_id = controller_container.id

            runtime = await provisioner.provision("session-raw-vnc")

            ports = client.containers.browser_containers[runtime.container_name].run_kwargs["ports"]
            self.assertEqual(ports, {"6080/tcp": ("127.0.0.1", None)}, f"isolated session publishes: {ports}")
            self.assertEqual(runtime.takeover_url, "http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale")
