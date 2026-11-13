"""The Codex host bridge's unit names, as an option an operator turns on, the places in the checkout where the
controller keeps a secret (N1): the bridge's Codex runs on the host, outside the CLI sandbox of app/cli_sandbox.py.

No CI runs a systemd unit. What is tested is the text: every path an option, with a dash, under the checkout the
unit works in; never the data directory or what holds the bridge's own socket; and each secret setting's default
place among them, so that a setting added to the sandbox's list is added here too.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from app.cli_sandbox import PROTECTED_ROOT, SECRET_PATH_SETTINGS
from app.config import Settings

UNIT = Path(__file__).resolve().parents[2] / "ops" / "systemd" / "codex-host-bridge.service"
OPTION = "#InaccessiblePaths="

pytestmark = pytest.mark.skipif(not UNIT.is_file(), reason="the image holds controller/app and controller/tests only")


def _lines() -> list[str]:
    return UNIT.read_text(encoding="utf-8").splitlines()


def _setting(name: str) -> str:
    (value,) = [line[len(name) + 1 :] for line in _lines() if line.startswith(name + "=")]
    return value


def _checkout() -> PurePosixPath:
    return PurePosixPath(_setting("WorkingDirectory"))


def _named() -> list[str]:
    """What the option's lines name, each as written (with its dash)."""
    return [line[len(OPTION) :] for line in _lines() if line.startswith(OPTION)]


def _closed() -> list[PurePosixPath]:
    return [PurePosixPath(path.removeprefix("-")) for path in _named()]


def test_the_paths_are_an_option_each_with_a_dash_and_each_in_the_checkout() -> None:
    named = _named()

    assert named, "the unit names no path"
    assert all(path.startswith("-") for path in named), "a path that is not there would stop the unit"
    assert all(path.is_relative_to(_checkout()) and path != _checkout() for path in _closed())
    # An option: nothing here is on until the operator takes the comment marks off. PrivateUsers= goes with them.
    assert not [line for line in _lines() if line.startswith(("InaccessiblePaths=", "PrivateUsers="))]
    assert "#PrivateUsers=yes" in _lines()


def test_neither_the_data_directory_nor_what_holds_the_bridges_socket_is_named() -> None:
    # systemd cannot nest a writable path inside an inaccessible one, and the bridge makes its socket there.
    arguments = _setting("ExecStart").split()
    socket = _checkout() / arguments[arguments.index("--socket-path") + 1]

    assert socket == _checkout() / "data" / "host-bridge" / "codex.sock"
    assert _checkout() / "data" not in _closed()
    assert not [path for path in _closed() if socket.is_relative_to(path)]


@pytest.mark.parametrize(
    "attribute", [name for name in sorted(SECRET_PATH_SETTINGS) if Settings.model_fields[name].default is not None]
)
def test_each_secret_settings_default_place_is_among_them(attribute: str) -> None:
    # The compose files mount the checkout's data directory at PROTECTED_ROOT.
    default = PurePosixPath(Settings.model_fields[attribute].default)
    place = _checkout() / "data" / default.relative_to(PROTECTED_ROOT)

    assert [path for path in _closed() if place.is_relative_to(path)], f"{SECRET_PATH_SETTINGS[attribute]} is open"


def test_the_checkouts_env_file_and_the_persona_file_env_example_suggests_are_among_them() -> None:
    # PROXY_PERSONA_FILE has no default: /data/proxy-personas.json is the place .env.example gives for it.
    assert {_checkout() / ".env", _checkout() / "data" / "proxy-personas.json"} <= set(_closed())
