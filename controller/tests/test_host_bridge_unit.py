"""The Codex host bridge's unit names, as an option an operator turns on, the places in the checkout where the
controller keeps a secret (N1, FXN1 U-6): the bridge's Codex runs on the host, outside the CLI sandbox of
app/cli_sandbox.py.

No CI job runs the unit (it was run by hand in user units on GitHub-hosted runners). What is tested is the text:
every path an option, with a dash, under the checkout the unit works in; never the data directory or what holds the
bridge's own socket; each secret setting's place among them, so that a setting added to the sandbox's list is added
here too; and the start-up check, commented like the rest, naming every path the option hides.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from app.cli_sandbox import PROTECTED_ROOT, SECRET_PATH_SETTINGS
from app.config import Settings

UNIT = Path(__file__).resolve().parents[2] / "ops" / "systemd" / "codex-host-bridge.service"
OPTION = "#InaccessiblePaths="
CHECK = "#ExecStartPre="
# The secret settings that have no default, by the place in the checkout the unit names for each (the one
# .env.example gives).
WITHOUT_A_DEFAULT = {"proxy_persona_file": "data/proxy-personas.json"}
# What the unit names that is no secret setting's: the checkout's .env, and the CLI home, which holds the sign-ins of
# the container's CLIs (the bridge's Codex signs in under the bridge's own home).
OTHER_PLACES = (".env", "data/cli-home")

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


def test_the_checkouts_env_file_the_cli_home_and_the_place_of_each_secret_without_a_default_are_among_them() -> None:
    # The parameters above pass over a setting with no default: each one has its place written here by hand.
    without = {name for name in SECRET_PATH_SETTINGS if Settings.model_fields[name].default is None}
    assert without == set(WITHOUT_A_DEFAULT), "a secret setting without a default has no place named in the unit"

    named = {_checkout() / place for place in (*OTHER_PLACES, *WITHOUT_A_DEFAULT.values())}
    assert named <= set(_closed())


def _check_line() -> str:
    (line,) = [line for line in _lines() if line.startswith(CHECK)]
    return line


def test_the_start_up_check_is_an_option_of_the_same_block_and_refuses_a_start_where_a_path_stays_readable() -> None:
    lines = _lines()
    line = _check_line()

    assert not [entry for entry in lines if entry.startswith("ExecStartPre=")], "on only with the option"
    assert lines.index("#PrivateUsers=yes") < lines.index(line) < lines.index("[Install]")
    assert line.startswith(CHECK + "/bin/sh -c 'for f in ")
    # A path that is not there passes ([ ! -r ]), as the dash of its InaccessiblePaths= line passes it over.
    assert line.endswith(
        '; do [ ! -r "$$f" ] || { echo "the option is on and $$f is still readable: this host does not let a user'
        " unit hide paths\" >&2; exit 78; }; done'"
    )


def test_the_start_up_check_names_every_path_the_option_hides_in_the_same_order() -> None:
    (checked,) = re.findall(r"for f in ([^;]*); do", _check_line())
    hidden = [str(path.relative_to(_checkout())) for path in _closed()]

    assert checked.split() == hidden
    assert "data/cli-home" in hidden
