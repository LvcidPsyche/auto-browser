"""The Codex host bridge's unit names, as an option an operator turns on, the places in the checkout where the
controller keeps a secret or a store (N1, FXN1 U-6): the bridge's Codex runs on the host, outside the CLI sandbox of
app/cli_sandbox.py.

No CI job runs the unit (it was run by hand in user units on GitHub-hosted runners): what is tested is its text,
the docs' install of it, and its start-up check, as the test names say.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from app.cli_sandbox import DERIVED_STORE_PLACES, PROTECTED_ROOT, SECRET_PATH_SETTINGS, STORE_PATH_SETTINGS
from app.config import Settings
from app.process_hardening import PRIVATE_TEMP_NAME

UNIT = Path(__file__).resolve().parents[2] / "ops" / "systemd" / "codex-host-bridge.service"
DOCS = Path(__file__).resolve().parents[2] / "docs" / "deployment.md"
# The checkout the unit works in, as shipped: a path for the operator to replace with their own as it is installed.
PLACEHOLDER = PurePosixPath("/path/to/auto-browser")
# How the docs install the unit: the copy gets the checkout's path, run from the checkout, in place of PLACEHOLDER.
INSTALL = f'sed "s|{PLACEHOLDER}|$PWD|g" ops/systemd/codex-host-bridge.service'
OPTION = "#InaccessiblePaths="
CHECK = "#ExecStartPre="
# The secret and store settings that have no default, by the place in the checkout the unit names for each: the
# one .env.example gives, and STATE_DB_PATH's as docs/deployment.md gives it.
WITHOUT_A_DEFAULT = {"proxy_persona_file": "data/proxy-personas.json", "state_db_path": "data/db/operator.db"}
# What the unit names that is no secret's or store's setting: the checkout's .env; the CLI home, which holds the
# sign-ins of the container's CLIs (the bridge's Codex signs in under the bridge's own home); and the controller's own
# temp directory (an OCR's screenshot, a local browser's profile).
OTHER_PLACES = (".env", "data/cli-home", f"data/{PRIVATE_TEMP_NAME}")
# The settings whose default place the option is to hide: each secret's and each store's.
KEPT_PATH_SETTINGS = SECRET_PATH_SETTINGS | STORE_PATH_SETTINGS

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


def _service() -> list[str]:
    lines = _lines()
    return lines[lines.index("[Service]") : lines.index("[Install]")]


def test_the_unit_works_in_a_placeholder_checkout_that_the_docs_install_it_with_the_operators_own() -> None:
    # A path of one developer's, copied as it was, ran the bridge in a checkout that is not there, or not yours.
    assert _checkout() == PLACEHOLDER
    docs = DOCS.read_text(encoding="utf-8")
    assert INSTALL in docs
    assert "cp ops/systemd/codex-host-bridge.service" not in docs, "a copy without the checkout's path"


def _install_steps() -> str:
    """The docs' install steps: the bash block that holds INSTALL."""
    text = DOCS.read_text(encoding="utf-8")
    (block,) = [block for block in re.findall(r"```bash\n(.*?)```", text, re.S) if INSTALL in block]
    return block


def _install_from(
    tmp_path: Path, checkout_name: str, run_in: str = "", sed: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    """The docs' install steps run as written by /bin/sh, from ``run_in`` in a checkout of this name holding the unit
    (its root where ``run_in`` is ""), with a home of its own (tmp_path/home, which a test may fill first), a
    systemctl stand-in that records its calls, and ``sed``, where given, a stand-in for sed's script: what sh said,
    the copy's path, and the calls."""
    checkout = tmp_path / checkout_name / "auto-browser"
    (checkout / "ops" / "systemd").mkdir(parents=True)
    (checkout / "ops" / "systemd" / UNIT.name).write_bytes(UNIT.read_bytes())
    (checkout / run_in).mkdir(exist_ok=True)
    stand_ins, home, calls = tmp_path / "bin", tmp_path / "home", tmp_path / "calls"
    stand_ins.mkdir()
    home.mkdir(exist_ok=True)
    (stand_ins / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n', encoding="utf-8")
    (stand_ins / "systemctl").chmod(0o755)
    if sed is not None:
        (stand_ins / "sed").write_text(f"#!/bin/sh\n{sed}\n", encoding="utf-8")
        (stand_ins / "sed").chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{stand_ins}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    done = subprocess.run(
        ["/bin/sh", "-c", _install_steps()], cwd=checkout / run_in, env=env, capture_output=True, text=True, timeout=60
    )
    made = home / ".config" / "systemd" / "user" / UNIT.name
    return done, made, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


@pytest.mark.skipif(os.name != "posix", reason="the install is a POSIX shell's")
def test_the_install_as_the_docs_write_it_puts_the_checkouts_path_in_every_place_of_the_placeholder(tmp_path) -> None:
    # B5-fix1 item 12 (C-6, OC-6): run as written, on a plain path.
    done, made, calls = _install_from(tmp_path, "plain")

    assert done.returncode == 0, done.stderr
    copy = made.read_text(encoding="utf-8")
    assert copy == UNIT.read_text(encoding="utf-8").replace(str(PLACEHOLDER), str(tmp_path / "plain" / "auto-browser"))
    # B5-fix3 item 8 (C3-10 = O3-5): enabled, not started: an upgrade carries its own lines over first.
    assert calls == ["--user daemon-reload", "--user enable codex-host-bridge.service"]
    assert [path.name for path in made.parent.iterdir()] == [UNIT.name], "the copy is rendered beside it, then moved"


@pytest.mark.skipif(os.name != "posix", reason="the install is a POSIX shell's")
@pytest.mark.parametrize("name", ["a&b", "a|b", "a\\b", "a%b", "a b", "a\tb", "a\nb", "a'b", 'a"b'])
def test_the_install_refuses_a_checkout_path_the_unit_cannot_take_before_it_makes_or_writes_anything(
    tmp_path, name
) -> None:
    # sed reads & | \ in its replacement as its own (a & put the placeholder back, a | stopped sed after > had emptied
    # the copy), systemd reads % as a specifier, and InaccessiblePaths= splits at a space, a tab or a line break
    # (H-17) and takes quotes off. B5-fix2 item 10 (OC2-13): nothing made either, the unit's directory among it.
    done, made, calls = _install_from(tmp_path, name)

    assert done.returncode != 0
    assert "the unit cannot take" in done.stderr
    assert not (tmp_path / "home" / ".config").exists() and calls == []


@pytest.mark.skipif(os.name != "posix", reason="the install is a POSIX shell's")
def test_the_install_run_from_another_directory_refuses_and_leaves_an_installed_copy_as_it_was(tmp_path) -> None:
    # B5-fix2 item 10 (H-16, OC2-2): run from controller/, as the Upgrade note's bullet before says to start the
    # controller, `> copy` emptied the installed copy, with the operator's own lines, before sed failed to read
    # ops/systemd/ there.
    installed = tmp_path / "home" / ".config" / "systemd" / "user" / UNIT.name
    installed.parent.mkdir(parents=True)
    installed.write_text("[Service]\nWorkingDirectory=/srv/auto-browser\nInaccessiblePaths=-/srv/x\n", encoding="utf-8")
    before = installed.read_bytes()

    done, made, calls = _install_from(tmp_path, "plain", run_in="controller")

    assert done.returncode != 0
    assert "checkout's root" in done.stderr, done.stderr
    assert made == installed and made.read_bytes() == before and calls == []
    assert [path.name for path in made.parent.iterdir()] == [UNIT.name]


@pytest.mark.skipif(os.name != "posix", reason="the install is a POSIX shell's")
def test_an_install_whose_sed_fails_part_way_leaves_an_installed_copy_as_it_was(tmp_path) -> None:
    # B5-fix3 item 8 (the orchestrator's mutant M20): run from the checkout's root, the guard above passes, and only
    # the render to a file beside the copy keeps a sed that fails part way (a full disk, a read error) from leaving
    # the copy, with the operator's own lines, cut short. What it wrote is left beside the copy, as the docs say.
    installed = tmp_path / "home" / ".config" / "systemd" / "user" / UNIT.name
    installed.parent.mkdir(parents=True)
    installed.write_text("[Service]\nWorkingDirectory=/srv/auto-browser\nInaccessiblePaths=-/srv/x\n", encoding="utf-8")
    before = installed.read_bytes()

    done, made, calls = _install_from(tmp_path, "plain", sed='head -c 40 "$2"; exit 1')

    assert done.returncode != 0
    assert made == installed and made.read_bytes() == before and calls == []
    left = made.parent / (UNIT.name + ".new")
    assert sorted(path.name for path in made.parent.iterdir()) == [UNIT.name, left.name]
    assert left.read_bytes() == UNIT.read_bytes()[:40]


def test_no_new_privileges_is_on_and_the_file_system_protections_are_options() -> None:
    # A setuid program the bridge's Codex starts gains nothing. ProtectSystem= and the rest would need every place
    # Codex writes to be named, which is not measured: they ship commented.
    service = _service()

    assert "NoNewPrivileges=yes" in service
    for name in ("ProtectSystem=", "ProtectHome=", "ReadWritePaths="):
        assert not [line for line in _lines() if line.startswith(name)], f"{name} is on"
        assert [line for line in service if line.startswith("#" + name)], f"{name} is not offered"


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
    "attribute", [name for name in sorted(KEPT_PATH_SETTINGS) if Settings.model_fields[name].default is not None]
)
def test_each_secret_and_store_settings_default_place_is_among_them(attribute: str) -> None:
    # The compose files mount the checkout's data directory at PROTECTED_ROOT. What the controller keeps in a store
    # (the approvals and their digest key, the sessions, the jobs, the peers the mesh trusts) the bridge's Codex is no
    # more to read or change than a secret.
    default = PurePosixPath(Settings.model_fields[attribute].default)
    place = _checkout() / "data" / default.relative_to(PROTECTED_ROOT)

    assert [path for path in _closed() if place.is_relative_to(path)], f"{KEPT_PATH_SETTINGS[attribute]} is open"


@pytest.mark.parametrize("name", sorted(DERIVED_STORE_PLACES))
def test_each_store_no_setting_names_is_among_them_at_its_default_place(name: str) -> None:
    # Beside its setting's default place, as the check finds it (cli_sandbox._derived_store_places).
    beside = PurePosixPath(Settings.model_fields[DERIVED_STORE_PLACES[name]].default).parent
    place = _checkout() / "data" / beside.relative_to(PROTECTED_ROOT) / name

    assert [path for path in _closed() if place.is_relative_to(path)], f"{name} is open"


def test_the_checkouts_env_file_the_cli_home_and_the_place_of_each_setting_without_a_default_are_among_them() -> None:
    # The parameters above pass over a setting with no default: each one has its place written here by hand.
    without = {name for name in KEPT_PATH_SETTINGS if Settings.model_fields[name].default is None}
    assert without == set(WITHOUT_A_DEFAULT), "a setting without a default has no place named in the unit"
    assert "STATE_DB_PATH=/" + WITHOUT_A_DEFAULT["state_db_path"] in DOCS.read_text(encoding="utf-8")

    for place in (*OTHER_PLACES, *WITHOUT_A_DEFAULT.values()):
        assert [path for path in _closed() if (_checkout() / place).is_relative_to(path)], f"{place} is open"


def _check_line() -> str:
    (line,) = [line for line in _lines() if line.startswith(CHECK)]
    return line


def test_the_start_up_check_is_an_option_of_the_same_block_and_refuses_a_path_that_can_be_read_or_searched() -> None:
    lines = _lines()
    line = _check_line()

    assert not [entry for entry in lines if entry.startswith("ExecStartPre=")], "on only with the option"
    assert lines.index("#PrivateUsers=yes") < lines.index(line) < lines.index("[Install]")
    assert line.startswith(CHECK + "/bin/sh -c 'for f in ")
    # A path that is not there passes (neither -r nor -x), as the dash of its InaccessiblePaths= line passes it over.
    assert line.endswith(
        '; do [ ! -r "$$f" ] && [ ! -x "$$f" ] || { echo "the option is on and $$f can still be read or searched:'
        " this host does not let a user unit hide paths\" >&2; exit 78; }; done'"
    )


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="the check is a POSIX shell's, over POSIX permissions, and to root every path there reads as readable",
)
@pytest.mark.parametrize(
    "mode, refused",
    [(0o700, True), (0o311, True), (0o100, True), (0o000, False), (None, False)],
    ids=["readable", "searchable, not readable", "searchable alone", "neither", "not there"],
)
def test_the_start_up_check_refuses_a_directory_left_readable_or_searchable(
    tmp_path: Path, mode: int | None, refused: bool
) -> None:
    # A directory that can be searched but not read still opens a file whose name is known (data/auth/profiles/...).
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    auth = checkout / "data" / "auth"
    if mode is not None:
        auth.mkdir(parents=True)
        auth.chmod(mode)
    (script,) = re.findall(r"^/bin/sh -c '(.*)'$", _check_line()[len(CHECK) :])

    try:
        done = subprocess.run(
            ["/bin/sh", "-c", script.replace("$$", "$")], cwd=checkout, capture_output=True, text=True, timeout=30
        )
    finally:
        if mode is not None:
            auth.chmod(0o700)

    assert done.returncode == (78 if refused else 0), done.stderr
    if refused:
        assert done.stderr == (
            "the option is on and data/auth can still be read or searched: this host does not let a user unit hide "
            "paths\n"
        )


def test_the_start_up_check_names_every_path_the_option_hides_in_the_same_order() -> None:
    (checked,) = re.findall(r"for f in ([^;]*); do", _check_line())
    hidden = [str(path.relative_to(_checkout())) for path in _closed()]

    assert checked.split() == hidden
    assert "data/cli-home" in hidden
