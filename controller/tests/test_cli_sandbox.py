"""Whether a provider CLI starts under the Landlock launcher, and what the controller does where it cannot (N1 and
its decision 5): app/cli_sandbox.py, and run_cli, the one place that starts a CLI.

The decisions are tested anywhere, with the probe stood in for. What a CLI that run_cli started can then open is
tested on Linux where Landlock works, as in test_cli_sandbox_launcher.py.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import cli_sandbox
from app.cli_sandbox_launcher import EXIT_NOT_APPLIED, MARKER
from app.config import Settings
from app.providers.base import CLIResult, ProviderAPIError
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter
from tests.test_cli_sandbox_launcher import LANDLOCK_ABI, STAND_IN, needs_landlock
from tests.test_harness_uv_output_cap import _is_running
from tests.test_run_cli import _HOLDER as HOLDS_THE_PIPES
from tests.test_run_cli import _LAUNCHER as STARTS_A_GRANDCHILD

# The settings decision 5 names: each holds the place of a secret of the controller's.
SECRET_SETTINGS = [
    "API_TOKEN_FILE",
    "TAKEOVER_TOKEN_FILE",
    "AUTH_ROOT",
    "WITNESS_ROOT",
    "MESH_IDENTITY_DIR",
    "ISOLATED_TUNNEL_KEY_PATH",
    "PROXY_PERSONA_FILE",
    "BROWSER_WS_ENDPOINT_FILE",
]
COMMAND = ["cli", "--flag"]
CLI_ENV = {"PATH": os.environ.get("PATH", ""), "PROVIDER_KEY": "value"}
_DECISION = '{"action":"done","reason":"x","risk_category":"read"}'


@pytest.fixture(autouse=True)
def _a_process_that_has_probed_and_reported_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe's answer, and each line already written, are kept for the process: every test starts without."""
    monkeypatch.setattr(cli_sandbox, "_asked", None)
    monkeypatch.setattr(cli_sandbox, "_reported", set())


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A protected root in tmp_path, laid out as /data is: the CLI home and two sessions' screenshots in it."""
    root = Path(os.path.realpath(tmp_path)) / "data"
    home = root / "cli-home"
    home.mkdir(parents=True)
    shots = [root / "artifacts" / session / "shot.png" for session in ("session-1", "session-2")]
    for shot in shots:
        shot.parent.mkdir(parents=True)
        shot.write_text("content of shot.png", encoding="utf-8")
    monkeypatch.setattr(cli_sandbox, "PROTECTED_ROOT", str(root))
    # The temp and device directories every CLI may write are this host's: the tests that are about them stand in
    # for them in tmp_path, and the others go without.
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", ())
    # The controller's own home, outside every directory a CLI is given unless a test puts it there.
    controller_home = root.parent / "homes" / "controller"
    controller_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(controller_home))
    monkeypatch.setenv("USERPROFILE", str(controller_home))
    return SimpleNamespace(
        root=root,
        home=home,
        shot=shots[0],
        other_shot=shots[1],
        outside=root.parent / "outside",
        controller_home=controller_home,
    )


def _settings(data: SimpleNamespace, **changed: str | None) -> Settings:
    """Settings that keep every secret in the protected root, but for what ``changed`` says."""
    inside = {
        "API_TOKEN_FILE": "api-token",
        "TAKEOVER_TOKEN_FILE": "takeover-token",
        "AUTH_ROOT": "auth",
        "WITNESS_ROOT": "witness",
        "MESH_IDENTITY_DIR": "mesh/identity",
        "ISOLATED_TUNNEL_KEY_PATH": "ssh/id_ed25519",
        "PROXY_PERSONA_FILE": "proxy-personas.json",
        "BROWSER_WS_ENDPOINT_FILE": "browser-profile/browser-ws-endpoint.txt",
        "CLI_HOME": "cli-home",
    }
    values: dict[str, str | None] = {name: str(data.root.joinpath(*path.split("/"))) for name, path in inside.items()}
    return Settings(_env_file=None, **(values | changed))


def _probe_says(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> list[dict[str, str]]:
    """Stand in for the probe: it answers ``answer`` (None: the launcher works) as the launcher's own answer, which is
    kept, and each run's environment is kept."""
    runs: list[dict[str, str]] = []

    def probe(env: dict[str, str]) -> tuple[str | None, bool]:
        runs.append(env)
        return answer, True

    monkeypatch.setattr(cli_sandbox, "_probe", probe)
    return runs


async def _launch(
    settings: Settings,
    data: SimpleNamespace,
    *,
    home: Path | None = None,
    image: Path | None = None,
    env: dict[str, str] | None = None,
):
    return await cli_sandbox.launch_command(
        settings,
        COMMAND,
        cli_home=str(home or data.home),
        image_path=str(image) if image else None,
        env=CLI_ENV if env is None else env,
    )


def _under_the_launcher(data: SimpleNamespace, *grants: str) -> list[str]:
    protect = ["--protect", str(data.root), "--rw", str(data.home)]
    return [sys.executable, "-I", "-S", cli_sandbox.LAUNCHER, *protect, *grants, "--", *COMMAND]


def _given_to_write(command: list[str]) -> list[str]:
    """The --rw directories of a launcher command."""
    assert command[3] == cli_sandbox.LAUNCHER, "not under the launcher"
    own = command[: command.index("--")]
    return [own[index + 1] for index, option in enumerate(own) if option == "--rw"]


def _directories(*paths: Path) -> list[str]:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return [str(path) for path in paths]


def _written(caplog: pytest.LogCaptureFixture, level: int = logging.WARNING) -> list[str]:
    """The lines app.cli_sandbox wrote at ``level``."""
    return [
        record.getMessage() for record in caplog.records if record.name == "app.cli_sandbox" and record.levelno == level
    ]


def _link_to_directory(link: Path, target: Path) -> None:
    """A link that a path resolves through: a junction on Windows, where a symbolic link takes a privilege."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


# ---- what the module names ----


def test_the_secret_settings_are_those_of_the_decision_each_a_setting_by_that_name() -> None:
    assert sorted(cli_sandbox.SECRET_PATH_SETTINGS.values()) == sorted(SECRET_SETTINGS)
    for attribute, name in cli_sandbox.SECRET_PATH_SETTINGS.items():
        field = Settings.model_fields[attribute]
        names = [field.alias] if field.alias else list(field.validation_alias.choices)
        assert name in names, f"{name} does not set Settings.{attribute}"


def test_every_secret_setting_that_has_a_default_has_it_in_the_protected_root() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)

    defaults = {name: getattr(settings, attribute) for attribute, name in cli_sandbox.SECRET_PATH_SETTINGS.items()}
    assert defaults.pop("PROXY_PERSONA_FILE") is None, "the one that is unset by default"
    for name, default in defaults.items():
        assert PurePosixPath(default).is_relative_to(cli_sandbox.PROTECTED_ROOT), name
    assert cli_sandbox.PROTECTED_ROOT == "/data"
    assert settings.cli_sandbox_required is False
    assert settings.cli_sandbox_enabled is True


def test_the_launcher_is_the_file_beside_the_module() -> None:
    launcher = Path(cli_sandbox.LAUNCHER)

    assert launcher.is_file() and launcher.is_absolute()
    assert (launcher.parent, launcher.name) == (Path(cli_sandbox.__file__).parent, "cli_sandbox_launcher.py")


# ---- decision 5, with the probe stood in for ----


async def test_with_every_secret_in_the_root_the_cli_starts_under_the_launcher_and_nothing_is_written(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)

    with caplog.at_level(logging.DEBUG):
        command = await _launch(_settings(data), data)

    assert command == _under_the_launcher(data)
    assert [record for record in caplog.records if record.name == "app.cli_sandbox"] == []


async def test_the_calls_image_is_the_one_file_the_cli_may_read_in_the_root(data, monkeypatch) -> None:
    _probe_says(monkeypatch, None)

    command = await _launch(_settings(data), data, image=data.shot)

    assert command == _under_the_launcher(data, "--ro-file", str(data.shot))
    assert str(data.other_shot) not in command and str(data.shot.parent) not in command


@pytest.mark.parametrize("setting", SECRET_SETTINGS)
async def test_a_secret_outside_the_root_is_one_warning_that_names_its_setting_and_the_cli_still_runs(
    data, monkeypatch, caplog, setting: str
) -> None:
    _probe_says(monkeypatch, None)
    settings = _settings(data, **{setting: str(data.outside / "kept-elsewhere")})

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(settings, data) for _ in range(3)]

    assert commands == [_under_the_launcher(data)] * 3, "it still runs, and under the launcher"
    (line,) = _written(caplog)
    assert setting in line
    assert not [name for name in SECRET_SETTINGS if name != setting and name in line]
    assert "kept-elsewhere" not in line and str(data.outside) not in line, "a name, never a value"


async def test_a_secret_in_the_cli_home_is_named_too(data, monkeypatch, caplog) -> None:
    # The home is the one directory of the root a CLI reads and writes.
    _probe_says(monkeypatch, None)

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, AUTH_ROOT=str(data.home / "auth")), data)

    (line,) = _written(caplog)
    assert "AUTH_ROOT" in line


@pytest.mark.parametrize("kind", ["a directory", "a file"])
async def test_a_secret_whose_path_is_written_inside_the_cli_home_is_named_wherever_it_resolves(
    data, monkeypatch, caplog, kind: str
) -> None:
    # The path resolves into the root, which the ruleset closes; but the CLI writes its home, so it can put a file of
    # its own making where the link is, and the controller would read that as its secret.
    if kind == "a file" and os.name == "nt":
        pytest.skip("a symbolic link to a file takes a privilege on Windows")
    _probe_says(monkeypatch, None)
    if kind == "a directory":
        (data.root / "auth").mkdir()
        _link_to_directory(data.home / "auth", data.root / "auth")
        name, changed = "AUTH_ROOT", {"AUTH_ROOT": str(data.home / "auth")}
    else:
        (data.root / "api-token").write_text("a token", encoding="utf-8")
        (data.home / "token").symlink_to(data.root / "api-token")
        name, changed = "API_TOKEN_FILE", {"API_TOKEN_FILE": str(data.home / "token")}

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, **changed), data)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data)

    (line,) = _written(caplog)
    assert name in line


@pytest.mark.parametrize("unset", [None, ""])
async def test_a_setting_that_is_not_set_is_passed_over(data, monkeypatch, caplog, unset) -> None:
    _probe_says(monkeypatch, None)

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, PROXY_PERSONA_FILE=unset), data)

    assert _written(caplog) == []


async def test_a_secret_whose_path_leads_through_a_link_out_of_the_root_is_outside(data, monkeypatch, caplog) -> None:
    # Written, the path is in the root. The ruleset closes places, and the place is elsewhere.
    _probe_says(monkeypatch, None)
    (data.outside / "auth").mkdir(parents=True)
    _link_to_directory(data.root / "auth", data.outside / "auth")

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data), data)

    (line,) = _written(caplog)
    assert "AUTH_ROOT" in line and "WITNESS_ROOT" not in line


async def test_a_root_that_is_a_link_to_another_place_protects_nothing_there(data, monkeypatch, caplog) -> None:
    # The ruleset closes the root as it is written. Where that is a link, the files are in another place, which the
    # ruleset leaves open: every secret is outside.
    _probe_says(monkeypatch, None)
    written_as = data.root.parent / "written-as"
    _link_to_directory(written_as, data.root)
    monkeypatch.setattr(cli_sandbox, "PROTECTED_ROOT", str(written_as))
    under_the_link = {name: str(written_as / name.lower()) for name in SECRET_SETTINGS}

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, **under_the_link), data, home=written_as / "cli-home")

    (line,) = _written(caplog)
    assert [name for name in SECRET_SETTINGS if name not in line] == []


@pytest.mark.parametrize("home", ["the root", "what holds the root"])
async def test_a_cli_home_that_is_the_root_or_holds_it_runs_the_cli_without_the_launcher_and_says_so(
    data, monkeypatch, caplog, home: str
) -> None:
    # The launcher refuses such a home: a rule on it would open the root.
    _probe_says(monkeypatch, None)
    cli_home = data.root if home == "the root" else data.root.parent

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(_settings(data), data, home=cli_home) for _ in range(2)]

    assert commands == [COMMAND, COMMAND]
    (line,) = _written(caplog)
    assert "CLI_HOME" in line


async def test_where_the_probe_fails_the_cli_runs_without_the_launcher_and_one_warning_says_why(
    data, monkeypatch, caplog
) -> None:
    runs = _probe_says(monkeypatch, "its probe exited 125")

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(_settings(data, AUTH_ROOT=str(data.outside)), data) for _ in range(3)]

    assert commands == [COMMAND] * 3
    (line,) = _written(caplog)
    assert "its probe exited 125" in line
    assert "AUTH_ROOT" not in line, "no setting is examined where nothing is protected"
    assert runs == [CLI_ENV], "probed once, with the environment the CLI gets"


async def test_the_probes_answer_is_kept_for_the_process(data, monkeypatch) -> None:
    runs = _probe_says(monkeypatch, None)

    for _ in range(3):
        await _launch(_settings(data), data)
    await asyncio.gather(*[_launch(_settings(data), data) for _ in range(3)])

    assert runs == [CLI_ENV]


async def test_calls_that_start_together_probe_once(data, monkeypatch) -> None:
    runs: list[dict[str, str]] = []

    def slow_probe(env: dict[str, str]) -> tuple[None, bool]:
        runs.append(env)
        time.sleep(0.2)
        return None, True

    monkeypatch.setattr(cli_sandbox, "_probe", slow_probe)

    commands = await asyncio.gather(*[_launch(_settings(data), data) for _ in range(4)])

    assert commands == [_under_the_launcher(data)] * 4
    assert len(runs) == 1


@pytest.mark.parametrize("condition", ["no landlock", "a secret outside the root", "a home that holds the root"])
async def test_with_cli_sandbox_required_each_condition_refuses_the_cli_and_is_one_error_line(
    data, monkeypatch, caplog, condition: str
) -> None:
    _probe_says(monkeypatch, "this is not Linux" if condition == "no landlock" else None)
    changed = {"WITNESS_ROOT": str(data.outside)} if condition == "a secret outside the root" else {}
    home = data.root.parent if condition == "a home that holds the root" else data.home
    settings = _settings(data, CLI_SANDBOX_REQUIRED="true", **changed)
    names = {
        "no landlock": "this is not Linux",
        "a secret outside the root": "WITNESS_ROOT",
        "a home that holds the root": "CLI_HOME",
    }

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(cli_sandbox.SandboxRequired):
                await _launch(settings, data, home=home)

    (line,) = _written(caplog, logging.ERROR)
    assert names[condition] in line and "CLI_SANDBOX_REQUIRED" in line
    assert str(data.outside) not in line, "a name, never a value"
    assert _written(caplog) == [], "the one line is the error"


async def test_with_cli_sandbox_required_and_nothing_wrong_the_cli_starts_under_the_launcher(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true"), data, image=data.shot)

    assert command == _under_the_launcher(data, "--ro-file", str(data.shot))
    assert _written(caplog) == _written(caplog, logging.ERROR) == []


# ---- CLI_SANDBOX_ENABLED ----


@pytest.mark.parametrize("enabled", [None, "true"], ids=["by default", "set to true"])
async def test_the_sandbox_is_on_by_default_and_with_cli_sandbox_enabled_true(data, monkeypatch, enabled) -> None:
    _probe_says(monkeypatch, None)
    changed = {} if enabled is None else {"CLI_SANDBOX_ENABLED": enabled}

    assert await _launch(_settings(data, **changed), data) == _under_the_launcher(data)


async def test_with_the_sandbox_turned_off_the_cli_starts_as_it_is_and_no_probe_is_started(
    data, monkeypatch, caplog
) -> None:
    # The way out for a CLI that cannot run under the ruleset: it reads what the controller's user can, as before.
    calls = _the_probe_runs(monkeypatch, AssertionError("a probe was started"))
    settings = _settings(data, CLI_SANDBOX_ENABLED="false")

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(settings, data) for _ in range(3)]

    assert commands == [COMMAND] * 3
    assert calls == [] and cli_sandbox._asked is None, "no probe was started, and no thread for one"
    (line,) = _written(caplog)
    assert "without the Landlock sandbox" in line and "CLI_SANDBOX_ENABLED is false" in line


async def test_with_the_sandbox_turned_off_and_required_no_cli_runs_and_one_error_says_why(
    data, monkeypatch, caplog
) -> None:
    calls = _the_probe_runs(monkeypatch, AssertionError("a probe was started"))
    settings = _settings(data, CLI_SANDBOX_ENABLED="false", CLI_SANDBOX_REQUIRED="true")
    adapter = ClaudeAdapter(settings)

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(cli_sandbox.SandboxRequired):
                await _launch(settings, data)
        with _spawning(_child()) as spawn, pytest.raises(ProviderAPIError) as caught:
            await adapter.run_cli(command=COMMAND, env=CLI_ENV, cwd=str(data.home))

    spawn.assert_not_awaited()
    assert caught.value.message == cli_sandbox.REQUIRED_MESSAGE
    (line,) = _written(caplog, logging.ERROR)
    assert "CLI_SANDBOX_REQUIRED" in line and "CLI_SANDBOX_ENABLED is false" in line
    assert _written(caplog) == [], "the one line is the error"
    assert calls == []


# ---- what a CLI may write: its home, the temp directories, /dev and CLI_SANDBOX_WRITABLE ----


def test_besides_its_home_a_cli_may_write_the_temp_directories_and_dev() -> None:
    assert cli_sandbox.SHARED_WRITABLE == ("/tmp", "/var/tmp", "/dev")
    assert cli_sandbox.TEMP_VARIABLES == ("TMPDIR", "TEMP", "TMP")


async def test_the_shared_directories_that_are_there_are_given_each_as_its_resolved_path(data, monkeypatch) -> None:
    _probe_says(monkeypatch, None)
    temp, devices, real_var_temp = _directories(
        data.outside / "tmp", data.outside / "dev", data.outside / "real-var-tmp"
    )
    _link_to_directory(data.outside / "var-tmp", Path(real_var_temp))
    shared = (temp, str(data.outside / "var-tmp"), str(data.outside / "not-there"), devices)
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", shared)

    command = await _launch(_settings(data), data)

    assert _given_to_write(command) == [str(data.home), temp, real_var_temp, devices]


@pytest.mark.parametrize("variable", ["TMPDIR", "TEMP", "TMP"])
@pytest.mark.parametrize("where", ["outside the root", "in the root outside the home", "inside the home"])
async def test_a_temp_directory_of_the_clis_environment_is_given_outside_the_root_and_once(
    data, monkeypatch, variable: str, where: str
) -> None:
    # One under the root outside the home stays closed, as it always was; one inside the home is the home's.
    _probe_says(monkeypatch, None)
    place = {
        "outside the root": data.outside / "temp",
        "in the root outside the home": data.root / "temp",
        "inside the home": data.home / "temp",
    }[where]
    _directories(place)

    command = await _launch(_settings(data), data, env={**CLI_ENV, variable: str(place)})

    expected = [str(data.home), str(place)] if where == "outside the root" else [str(data.home)]
    assert _given_to_write(command) == expected


async def test_the_clis_home_variable_is_given_once_where_it_is_the_cli_home(data, monkeypatch) -> None:
    # As it is wherever CLI_HOME is set: here a home outside the root, as a host-subscriptions compose has it.
    _probe_says(monkeypatch, None)
    (home,) = _directories(data.outside / "botuser")

    command = await _launch(_settings(data, CLI_HOME=home), data, home=Path(home), env={**CLI_ENV, "HOME": home})

    assert command == [*_under_the_launcher(data)[:6], "--rw", home, "--", *COMMAND]


async def test_a_directory_cli_sandbox_writable_names_is_given(data, monkeypatch, caplog) -> None:
    _probe_says(monkeypatch, None)
    (state,) = _directories(data.outside / "codex-state")

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, CLI_SANDBOX_WRITABLE=f" {state} ,"), data)

    assert _given_to_write(command) == [str(data.home), state]
    assert _written(caplog) == []


async def test_an_entry_of_cli_sandbox_writable_that_is_no_directory_is_passed_over_with_one_warning(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)
    missing = data.outside / "not-there"
    settings = _settings(data, CLI_SANDBOX_WRITABLE=str(missing))

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(settings, data) for _ in range(3)]

    assert commands == [_under_the_launcher(data)] * 3
    (line,) = _written(caplog)
    assert "CLI_SANDBOX_WRITABLE" in line and str(missing) in line


async def test_an_entry_of_cli_sandbox_writable_in_the_root_is_given_and_a_secret_in_it_is_open(
    data, monkeypatch, caplog
) -> None:
    # The operator's to choose; but what the CLI may write there it can replace, a secret's file among it.
    _probe_says(monkeypatch, None)
    (shared,) = _directories(data.root / "shared")

    with caplog.at_level(logging.WARNING):
        command = await _launch(
            _settings(data, CLI_SANDBOX_WRITABLE=shared, WITNESS_ROOT=str(data.root / "shared" / "witness")), data
        )

    assert _given_to_write(command) == [str(data.home), shared]
    (line,) = _written(caplog)
    assert "WITNESS_ROOT" in line and "AUTH_ROOT" not in line


async def test_of_two_directories_one_inside_the_other_only_the_outer_is_given(data, monkeypatch) -> None:
    # The outer rule covers the inner, and the inner is the one a CLI could swap for a link.
    _probe_says(monkeypatch, None)
    outer, inner = _directories(data.outside / "outer", data.outside / "outer" / "inner")
    real = Path(_directories(data.outside / "real")[0])
    _link_to_directory(data.outside / "outer" / "to-real", real)
    entries = ",".join([inner, outer, str(data.outside / "outer" / "to-real"), str(real)])

    command = await _launch(_settings(data, CLI_SANDBOX_WRITABLE=entries), data)

    # to-real lies in outer as written: no rule of its own, and real itself is given as written.
    assert _given_to_write(command) == [str(data.home), outer, str(real)]


@pytest.mark.parametrize(
    "layout", ["an alias of /tmp", "an operator's link to /tmp", "an operator's link from the root", "no link"]
)
async def test_a_directory_written_through_a_link_into_a_directory_already_given_lies_in_it(
    data, monkeypatch, layout: str
) -> None:
    # A link part-way along a written path (/var/tmp linked to /tmp, which is given only resolved, or an operator's
    # link) leads into a directory a CLI may write, and what lies past it there a CLI of an earlier call can have
    # swapped for a link of its own: to the controller's user site, or into the root. Its rule is that directory's.
    _probe_says(monkeypatch, None)
    (temp,) = _directories(data.outside / "tmp")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (temp,))
    site = Path(_directories(data.controller_home / ".local" / "lib" / "python3.12" / "site-packages")[0])
    env, home, changed = CLI_ENV, data.home, {}
    if layout == "an alias of /tmp":
        alias = data.outside / "var-tmp"
        _link_to_directory(alias, Path(temp))
        _link_to_directory(Path(temp) / "work", site)
        monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (temp, str(alias)))  # production's order
        env = {**CLI_ENV, "TMPDIR": str(alias / "work")}
    elif layout == "an operator's link to /tmp":
        _link_to_directory(data.outside / "opt-link", Path(temp))
        _link_to_directory(Path(temp) / "work", site)
        changed = {"CLI_SANDBOX_WRITABLE": str(data.outside / "opt-link" / "work")}
    elif layout == "an operator's link from the root":
        (moved,) = _directories(Path(temp) / "x")
        _link_to_directory(data.root / "x", Path(moved))
        _link_to_directory(Path(moved) / "home", Path(_directories(data.root / "auth")[0]))
        home = data.root / "x" / "home"
        changed = {"CLI_HOME": str(home)}
    else:
        env = {**CLI_ENV, "TMPDIR": _directories(Path(temp) / "work")[0]}

    command = await _launch(_settings(data, **changed), data, home=home, env=env)

    assert _given_to_write(command) == ([temp] if home != data.home else [str(data.home), temp])


@pytest.mark.parametrize(
    "entry", ["relative/dir", "/", "/data", "/data/", "/data/../", "//", "/./data"], ids=lambda entry: repr(entry)
)
def test_cli_sandbox_writable_refuses_a_relative_path_and_the_data_directory_or_what_holds_it(entry: str) -> None:
    with pytest.raises(ValueError, match="CLI_SANDBOX_WRITABLE") as refused:
        Settings(_env_file=None, CLI_SANDBOX_WRITABLE=entry)

    assert repr(entry) in str(refused.value)


def test_cli_sandbox_writable_takes_absolute_paths_in_the_data_directory_and_outside_it(tmp_path: Path) -> None:
    # Absolute as this platform has it: Windows takes /data/shared for one from Python 3.13 on.
    entries = [str(tmp_path / "state"), str(tmp_path / "data")] if os.name == "nt" else ["/srv/state", "/data/shared"]

    settings = Settings(_env_file=None, CLI_SANDBOX_WRITABLE=" , ".join(entries))

    assert settings.cli_sandbox_writable_paths == entries
    assert Settings(_env_file=None).cli_sandbox_writable_paths == []


@pytest.mark.parametrize("swap", ["a directory in the root", "what holds the root"])
async def test_a_home_a_cli_swapped_for_a_link_is_not_given_and_the_cli_still_starts_under_the_launcher(
    data, monkeypatch, caplog, swap: str
) -> None:
    # CLI_HOME lies in a directory the CLI may write: it moves its home aside and puts a link in its place. Resolved,
    # the home is then a directory of the root, or a directory that holds the root (/ in production).
    _probe_says(monkeypatch, None)
    (temp,) = _directories(data.outside / "tmp")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (temp,))
    approvals = Path(_directories(data.root / "approvals")[0])
    target = approvals if swap == "a directory in the root" else data.root.parent
    home = Path(temp) / "cli-home"
    _link_to_directory(home, target)
    settings = _settings(data, CLI_HOME=str(home))

    with caplog.at_level(logging.WARNING):
        command = await _launch(settings, data, home=home)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_HOME=str(home), CLI_SANDBOX_REQUIRED="true"), data, home=home)

    assert _given_to_write(command) == [temp], "neither the home's place nor anything holding the root"
    (line,) = _written(caplog)
    assert "CLI_HOME" in line and str(home) in line and str(target) in line


@pytest.mark.parametrize("how", ["CLI_HOME is blank", "CLI_HOME is the controller's home", "CLI_SANDBOX_WRITABLE"])
async def test_where_a_cli_may_write_the_controllers_home_one_warning_says_so_and_required_refuses(
    data, monkeypatch, caplog, how: str
) -> None:
    # What a CLI leaves in the controller's home (a Python user site, an ssh config) the controller, which is not
    # under the sandbox, runs at its next start.
    _probe_says(monkeypatch, None)
    private = data.controller_home / ".cache" / "auto-browser" / "cli-home"
    _directories(private)
    env, home, changed = CLI_ENV, data.home, {}
    if how == "CLI_HOME is blank":  # the CLI's HOME is then the controller's, and its home a directory in it
        env, home, changed = {**CLI_ENV, "HOME": str(data.controller_home)}, private, {"CLI_HOME": ""}
    elif how == "CLI_HOME is the controller's home":
        home, changed = data.controller_home, {"CLI_HOME": str(data.controller_home)}
    else:
        changed = {"CLI_SANDBOX_WRITABLE": str(data.controller_home.parent)}

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(_settings(data, **changed), data, home=home, env=env) for _ in range(2)]
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data, home=home, env=env)

    assert commands[0] == commands[1] and commands[0][3] == cli_sandbox.LAUNCHER, "it runs, under the launcher"
    (line,) = _written(caplog)
    assert str(data.controller_home) in line and "next start" in line
    (refusal,) = _written(caplog, logging.ERROR)
    assert str(data.controller_home) in refusal


async def test_where_the_controllers_home_and_a_secret_are_both_open_the_home_is_named(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)
    settings = _settings(data, CLI_SANDBOX_WRITABLE=str(data.controller_home), AUTH_ROOT=str(data.outside / "auth"))

    with caplog.at_level(logging.WARNING):
        await _launch(settings, data)

    (line,) = _written(caplog)
    assert str(data.controller_home) in line and "AUTH_ROOT" not in line


# ---- the probe ----


def _the_probe_runs(monkeypatch: pytest.MonkeyPatch, *outcomes) -> list[SimpleNamespace]:
    """Stand in for subprocess.run under the probe, on what is taken for Linux. Each run takes the next of
    ``outcomes`` (the last one repeats): what it returns, the error it raises, or a function it calls and whose
    result it returns. Each call is kept."""
    calls: list[SimpleNamespace] = []

    def run(command, **kwargs):
        calls.append(SimpleNamespace(command=command, **kwargs))
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome() if callable(outcome) else outcome

    monkeypatch.setattr(cli_sandbox, "_LINUX", True)
    monkeypatch.setattr(cli_sandbox.subprocess, "run", run)
    return calls


async def _until(condition, seconds: float = 10) -> None:
    """Wait for ``condition``, and fail rather than hang where it does not come."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "what the test waits for did not come"
        await asyncio.sleep(0.01)


_PASSED = subprocess.CompletedProcess([], 0, b"", b"")
_REFUSAL_LINE = MARKER + "landlock_create_ruleset(version): ENOSYS (Function not implemented)\n"
_REFUSED = subprocess.CompletedProcess([], EXIT_NOT_APPLIED, b"", _REFUSAL_LINE.encode())
# Probes that ended without the launcher's own answer.
_UNFINISHED = {
    "timed out": subprocess.TimeoutExpired(["python"], 30),
    "not started": OSError(errno.EAGAIN, "Resource temporarily unavailable"),
    "a traceback": subprocess.CompletedProcess([], 1, b"", b"Traceback (most recent call last):\n"),
    "125 without the launcher's line": subprocess.CompletedProcess([], EXIT_NOT_APPLIED, b"", b"Killed\n"),
}


def test_the_probe_runs_the_launcher_itself_in_its_probe_mode(monkeypatch) -> None:
    calls = _the_probe_runs(monkeypatch, _PASSED)

    assert cli_sandbox._probe(CLI_ENV) == (None, True)

    (call,) = calls
    probe = [sys.executable, "-I", "-S", cli_sandbox.LAUNCHER, "--protect", cli_sandbox.PROTECTED_ROOT, "--probe"]
    assert call.command == probe
    assert call.env == CLI_ENV and call.stdin == subprocess.DEVNULL
    assert call.timeout == cli_sandbox.PROBE_TIMEOUT_SECONDS
    # Without the pipes a refused probe's stderr is None, and reading it raises outside the probe's own try.
    assert call.capture_output is True and call.check is False


def test_a_probe_that_exits_with_the_launchers_status_is_a_kept_reason_that_holds_its_line(monkeypatch) -> None:
    _the_probe_runs(monkeypatch, _REFUSED)

    reason, kept = cli_sandbox._probe(CLI_ENV)

    assert reason is not None and "ENOSYS" in reason and str(EXIT_NOT_APPLIED) in reason
    assert reason.splitlines() == [reason], "one line of the log"
    assert kept is True, "the launcher's own refusal"


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired(["python"], 30), FileNotFoundError(2, "No such file or directory"), OSError(12, "x")],
    ids=["no answer in time", "no python", "no memory"],
)
def test_a_probe_that_cannot_be_run_is_a_reason_not_kept_and_not_an_error(monkeypatch, failure: Exception) -> None:
    _the_probe_runs(monkeypatch, failure)

    reason, kept = cli_sandbox._probe(CLI_ENV)

    assert reason and reason.splitlines() == [reason]
    assert kept is False


def test_off_linux_the_probe_starts_nothing_and_its_answer_is_kept(monkeypatch) -> None:
    calls = _the_probe_runs(monkeypatch, AssertionError("started a process"))
    monkeypatch.setattr(cli_sandbox, "_LINUX", False)

    reason, kept = cli_sandbox._probe(CLI_ENV)

    assert reason and kept is True
    assert calls == []


def test_this_process_takes_linux_for_linux_and_nothing_else() -> None:
    assert cli_sandbox._LINUX is sys.platform.startswith("linux")


# ---- which of the probe's answers is kept ----


@pytest.mark.parametrize("unfinished", list(_UNFINISHED))
async def test_a_probe_that_did_not_finish_is_asked_again_at_the_next_call(
    data, monkeypatch, caplog, unfinished: str
) -> None:
    # A loaded host at start-up, a transient OSError: not the kernel's answer, and not to be kept for the process.
    calls = _the_probe_runs(monkeypatch, _UNFINISHED[unfinished], _PASSED)

    with caplog.at_level(logging.WARNING):
        commands = [await _launch(_settings(data), data) for _ in range(3)]

    assert commands[0] == COMMAND, "the call it answered starts without the launcher, as before"
    assert commands[1:] == [_under_the_launcher(data)] * 2
    assert len(calls) == 2, "asked again at the next call, and the answer that came then is kept"
    (line,) = _written(caplog)
    assert "probe" in line


@pytest.mark.parametrize("answer", ["passed", "refused"])
async def test_the_launchers_own_answer_is_kept_for_the_process(data, monkeypatch, answer: str) -> None:
    calls = _the_probe_runs(monkeypatch, _PASSED if answer == "passed" else _REFUSED, AssertionError("probed again"))

    commands = [await _launch(_settings(data), data) for _ in range(3)]

    assert commands == [_under_the_launcher(data) if answer == "passed" else COMMAND] * 3
    assert len(calls) == 1


async def test_calls_that_start_together_share_one_unfinished_probe_and_a_later_call_asks_again(
    data, monkeypatch
) -> None:
    probing, release = threading.Event(), threading.Event()

    def held():
        probing.set()
        release.wait(30)
        raise subprocess.TimeoutExpired(["python"], 30)

    calls = _the_probe_runs(monkeypatch, held, _PASSED)
    together = [asyncio.create_task(_launch(_settings(data), data)) for _ in range(4)]
    try:
        await _until(probing.is_set)
    finally:
        release.set()
    commands = await asyncio.wait_for(asyncio.gather(*together), 10)

    # Each took the answer it waited for: four calls behind a timeout wait once, not four times in turn.
    assert commands == [COMMAND] * 4
    assert len(calls) == 1
    assert await _launch(_settings(data), data) == _under_the_launcher(data)
    assert len(calls) == 2


async def test_calls_waiting_for_the_probe_leave_the_loops_executor_to_the_rest_of_the_controller(
    data, monkeypatch
) -> None:
    # The job store, OCR, audit and approvals go through asyncio.to_thread: a probe that is asked again may run for
    # PROBE_TIMEOUT_SECONDS, and the calls behind it must not hold that executor's threads meanwhile.
    probing, release = threading.Event(), threading.Event()

    def held():
        probing.set()
        release.wait(30)
        return _PASSED

    _the_probe_runs(monkeypatch, held)
    executor = concurrent.futures.ThreadPoolExecutor(2)
    asyncio.get_running_loop().set_default_executor(executor)
    together = [asyncio.create_task(_launch(_settings(data), data)) for _ in range(4)]
    try:
        await _until(probing.is_set)
        await asyncio.wait_for(asyncio.to_thread(lambda: None), 5)
    finally:
        release.set()
        commands = await asyncio.wait_for(asyncio.gather(*together), 10)
        executor.shutdown(wait=False)

    assert commands == [_under_the_launcher(data)] * 4


async def test_a_waiting_call_that_is_cancelled_leaves_the_probe_to_the_calls_that_wait_with_it(
    data, monkeypatch
) -> None:
    probing, release = threading.Event(), threading.Event()

    def held():
        probing.set()
        release.wait(30)
        return _PASSED

    calls = _the_probe_runs(monkeypatch, held)
    first, second = (asyncio.create_task(_launch(_settings(data), data)) for _ in range(2))
    try:
        await _until(probing.is_set)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()

    assert await asyncio.wait_for(second, 10) == _under_the_launcher(data)
    assert await _launch(_settings(data), data) == _under_the_launcher(data)
    assert len(calls) == 1, "one probe for the calls that started together, and its answer kept"


async def test_a_probe_whose_thread_cannot_be_started_fails_that_call_and_the_next_call_probes(
    data, monkeypatch
) -> None:
    calls = _the_probe_runs(monkeypatch, _PASSED)
    real_start = threading.Thread.start
    refusals = iter([RuntimeError("can't start new thread")])

    def start(thread: threading.Thread) -> None:
        refused = next(refusals, None)
        if refused is not None:
            raise refused
        real_start(thread)

    executor = concurrent.futures.ThreadPoolExecutor(1)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(threading.Thread, "start", start)
            with pytest.raises(RuntimeError, match="can't start new thread"):
                await asyncio.wait_for(_launch(_settings(data), data), 5)
            assert await asyncio.wait_for(_launch(_settings(data), data), 5) == _under_the_launcher(data)
    finally:
        executor.shutdown(wait=False)

    assert len(calls) == 1


async def test_a_probe_that_raises_fails_its_call_keeps_nothing_and_the_next_call_probes_again(
    data, monkeypatch
) -> None:
    calls = _the_probe_runs(monkeypatch, ValueError("a probe that raised"), _PASSED)

    with pytest.raises(ValueError, match="a probe that raised"):
        await asyncio.wait_for(_launch(_settings(data), data), 5)
    second = await asyncio.wait_for(_launch(_settings(data), data), 5)
    third = await asyncio.wait_for(_launch(_settings(data), data), 5)

    assert second == third == _under_the_launcher(data)
    assert len(calls) == 2, "the failed probe is not kept, and the one that answered is"


# ---- run_cli ----


def _finished_stream(data: bytes) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    stream.feed_data(data)
    stream.feed_eof()
    return stream


def _child(returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b"") -> SimpleNamespace:
    """What run_cli reads of a child: its two pipes, and wait() for its exit."""
    return SimpleNamespace(
        stdin=None,
        stdout=_finished_stream(stdout),
        stderr=_finished_stream(stderr),
        wait=AsyncMock(return_value=returncode),
        returncode=returncode,
    )


def _spawning(child: SimpleNamespace):
    return patch("asyncio.create_subprocess_exec", AsyncMock(return_value=child))


async def test_run_cli_starts_the_cli_under_the_launcher_with_the_clis_own_environment_and_directory(
    data, monkeypatch
) -> None:
    runs = _probe_says(monkeypatch, None)
    adapter = ClaudeAdapter(_settings(data))
    directory = str(data.home / "call")

    with _spawning(_child()) as spawn:
        result = await adapter.run_cli(command=COMMAND, env=CLI_ENV, cwd=directory)

    assert list(spawn.await_args.args) == _under_the_launcher(data)
    assert spawn.await_args.kwargs["env"] == CLI_ENV and spawn.await_args.kwargs["cwd"] == directory
    assert runs == [CLI_ENV]
    assert (result.command, result.stdout) == (COMMAND, "ok"), "the result names the CLI's command, not the launcher"


async def test_run_cli_grants_the_image_it_is_passed_and_nothing_without_one(data, monkeypatch) -> None:
    _probe_says(monkeypatch, None)
    adapter = OpenAIAdapter(_settings(data))

    with _spawning(_child()) as with_image:
        await adapter.run_cli(command=COMMAND, env=CLI_ENV, image_path=str(data.shot))
    with _spawning(_child()) as without:
        await adapter.run_cli(command=COMMAND, env=CLI_ENV)

    assert list(with_image.await_args.args) == _under_the_launcher(data, "--ro-file", str(data.shot))
    assert list(without.await_args.args) == _under_the_launcher(data)


async def test_a_call_cancelled_while_the_probe_runs_starts_no_cli_and_the_probes_answer_is_kept(
    data, monkeypatch
) -> None:
    probing, answered = threading.Event(), threading.Event()

    def held_probe(env: dict[str, str]) -> tuple[None, bool]:
        probing.set()
        answered.wait(30)
        return None, True

    monkeypatch.setattr(cli_sandbox, "_probe", held_probe)
    adapter = ClaudeAdapter(_settings(data))

    with _spawning(_child()) as spawn:
        task = asyncio.create_task(adapter.run_cli(command=COMMAND, env=CLI_ENV))
        try:
            await _until(probing.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            answered.set()
        await _until(lambda: cli_sandbox._asked is not None and cli_sandbox._asked.done())

    spawn.assert_not_awaited()
    assert cli_sandbox._asked.result() is None, "the probe ran to its end, and the next call does not probe again"


async def test_run_cli_under_cli_sandbox_required_starts_nothing_and_raises_the_fixed_provider_error(
    data, monkeypatch
) -> None:
    _probe_says(monkeypatch, "this is not Linux")
    adapter = GeminiAdapter(_settings(data, CLI_SANDBOX_REQUIRED="true"))

    with _spawning(_child()) as spawn, pytest.raises(ProviderAPIError) as caught:
        await adapter.run_cli(command=COMMAND, env=CLI_ENV)

    spawn.assert_not_awaited()
    assert caught.value.message == cli_sandbox.REQUIRED_MESSAGE
    assert (caught.value.provider, caught.value.retryable, caught.value.status_code) == ("gemini", False, None)
    assert "/" not in cli_sandbox.REQUIRED_MESSAGE and "\\" not in cli_sandbox.REQUIRED_MESSAGE, "it names no path"


async def test_a_launch_that_ends_with_the_launchers_status_is_an_error_for_that_call_and_never_a_second_start(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)
    adapter = ClaudeAdapter(_settings(data))
    line = MARKER + "landlock_add_rule: EINVAL (Invalid argument)\n"

    with (
        _spawning(_child(EXIT_NOT_APPLIED, b"", line.encode())) as spawn,
        caplog.at_level(logging.ERROR, "app.providers.base"),
        pytest.raises(ProviderAPIError) as caught,
    ):
        await adapter.run_cli(command=COMMAND, env=CLI_ENV, image_path=str(data.shot))

    assert spawn.await_count == 1, "no second start, with or without the launcher"
    assert caught.value.message == cli_sandbox.NOT_APPLIED_MESSAGE and caught.value.retryable is False
    assert "/" not in cli_sandbox.NOT_APPLIED_MESSAGE and "\\" not in cli_sandbox.NOT_APPLIED_MESSAGE
    # What failed is in the log, where the operator reads it.
    assert "landlock_add_rule: EINVAL (Invalid argument)" in caplog.text


async def test_a_cli_that_exits_125_itself_fails_as_any_cli_does(data, monkeypatch) -> None:
    _probe_says(monkeypatch, None)
    adapter = ClaudeAdapter(_settings(data))

    with _spawning(_child(EXIT_NOT_APPLIED, b"", b"boom: bad flag")), pytest.raises(ProviderAPIError) as caught:
        await adapter.run_cli(command=COMMAND, env=CLI_ENV)

    assert caught.value.message == "boom: bad flag"


async def test_without_the_launcher_its_line_is_only_what_a_cli_printed(data, monkeypatch) -> None:
    _probe_says(monkeypatch, "this is not Linux")
    adapter = ClaudeAdapter(_settings(data))
    line = MARKER + "landlock_restrict_self: EPERM (Operation not permitted)"

    with _spawning(_child(EXIT_NOT_APPLIED, b"", line.encode())) as spawn, pytest.raises(ProviderAPIError) as caught:
        await adapter.run_cli(command=COMMAND, env=CLI_ENV)

    assert list(spawn.await_args.args) == COMMAND
    assert caught.value.message == line


@pytest.mark.parametrize("with_screenshot", [True, False])
async def test_only_the_codex_cli_is_given_the_calls_screenshot(tmp_path: Path, with_screenshot: bool) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    observation = {"url": "https://example.com", "title": "Example", "interactables": []}
    if with_screenshot:
        observation["screenshot_path"] = str(shot)
    home = str(tmp_path / "cli-home")
    adapters = {
        "claude": ClaudeAdapter(Settings(_env_file=None, CLAUDE_AUTH_MODE="cli", CLI_HOME=home)),
        "gemini": GeminiAdapter(Settings(_env_file=None, GEMINI_AUTH_MODE="cli", CLI_HOME=home)),
        "openai": OpenAIAdapter(
            Settings(_env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=home)
        ),
    }
    given: dict[str, str | None] = {}

    for name, adapter in adapters.items():

        async def fake_run_cli(*, command, name=name, **kwargs):
            given[name] = kwargs.get("image_path")
            if "--output-last-message" in command:
                Path(command[command.index("--output-last-message") + 1]).write_text(_DECISION, encoding="utf-8")
            return CLIResult(command=command, stdout=_DECISION, stderr="", returncode=0)

        with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
            await adapter._decide(
                goal="g", observation=observation, context_hints=None, previous_steps=[], model_override=None
            )

    assert given == {"claude": None, "gemini": None, "openai": str(shot) if with_screenshot else None}


# ---- the real kernel ----


@needs_landlock
def test_the_probe_passes_where_landlock_works(data) -> None:
    assert cli_sandbox._probe(CLI_ENV) == (None, True)


@needs_landlock
def test_the_probe_passes_for_the_root_production_protects() -> None:
    # /data is there in the image and is not on a runner: the ruleset is applied whole either way.
    assert cli_sandbox.PROTECTED_ROOT == "/data"

    assert cli_sandbox._probe(CLI_ENV) == (None, True)


def _spy_on_spawns():
    spawned: list[tuple] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        spawned.append(args)
        return await real(*args, **kwargs)

    return spawned, patch("app.providers.base.asyncio.create_subprocess_exec", spy)


@needs_landlock
async def test_a_cli_that_run_cli_started_reads_its_home_and_its_screenshot_and_no_secret(data) -> None:
    settings = _settings(data)
    secrets = [Path(getattr(settings, attribute)) for attribute in cli_sandbox.SECRET_PATH_SETTINGS]
    for secret in secrets:
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("a secret", encoding="utf-8")
    steps = [
        *[["read", str(secret)] for secret in secrets],
        ["list", str(data.root)],
        ["read", str(data.shot)],
        ["read", str(data.other_shot)],
        ["create", str(data.home / "written-by-the-cli")],
    ]
    adapter = OpenAIAdapter(settings)
    spawned, spy = _spy_on_spawns()

    with spy:
        result = await adapter.run_cli(
            command=[sys.executable, "-c", STAND_IN, json.dumps(steps)],
            env=CLI_ENV,
            cwd=str(data.home),
            image_path=str(data.shot),
        )

    assert spawned[0][3] == cli_sandbox.LAUNCHER, "the CLI was started under the launcher"
    expected = [*["EACCES"] * len(SECRET_SETTINGS), "EACCES", "content of shot.png", "EACCES", "ok"]
    assert json.loads(result.stdout)["results"] == expected
    assert (data.home / "written-by-the-cli").is_file()


@needs_landlock
async def test_under_the_list_launch_command_builds_a_cli_writes_its_own_directories_and_nothing_beside_the_root(
    data, monkeypatch
) -> None:
    # Wherever tmp_path lies under /tmp, /tmp holds this test's root and is given to no CLI: a directory beside the
    # root stands in for it. /dev is the real one.
    (temp,) = _directories(data.outside / "tmp")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (temp, "/dev"))
    beside = Path(_directories(data.outside / "beside")[0])
    note, program = beside / "note.txt", beside / "program"
    note.write_text("content of note.txt", encoding="utf-8")
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    works = [
        ["create", str(Path(temp) / "made")],
        ["write", "/dev/null"],
        ["pty"],
        ["create", str(data.home / "made")],
        ["read", str(note)],
        ["list", str(beside)],
        ["run", str(program)],
    ]
    refused = [
        ["create", str(beside / "new")],
        ["append", str(note)],
        ["remove", str(note)],
        ["rename", str(note), str(beside / "renamed")],
        ["mkdir", str(beside / "new-directory")],
    ]
    if LANDLOCK_ABI >= 3:
        refused.append(["truncate", str(note)])
    cli = [sys.executable, "-c", STAND_IN, json.dumps(works + refused)]

    command = await cli_sandbox.launch_command(
        _settings(data), cli, cli_home=str(data.home), image_path=None, env=CLI_ENV
    )
    done = subprocess.run(  # noqa: S603 - the launcher and this interpreter
        command, env=CLI_ENV, cwd=data.home, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120
    )

    assert _given_to_write(command) == [str(data.home), temp, "/dev"]
    assert done.returncode == 0, done.stderr[-2000:]
    results = json.loads(done.stdout)["results"]
    assert results == ["ok"] * 4 + ["content of note.txt", "ok", "ok"] + ["EACCES"] * len(refused)
    assert note.read_text(encoding="utf-8") == "content of note.txt"


@needs_landlock
async def test_a_timeout_kills_a_grandchild_of_a_cli_started_under_the_launcher(data) -> None:
    # The launcher leads the process group and the CLI it execs keeps it: the kill reaches what the CLI started.
    settings = _settings(data)
    settings.model_request_timeout_seconds = 5
    adapter = ClaudeAdapter(settings)
    pid_file = data.home / "grandchild.pid"
    spawned, spy = _spy_on_spawns()

    try:
        with spy, pytest.raises(ProviderAPIError) as caught:
            await adapter.run_cli(
                command=[sys.executable, "-c", STARTS_A_GRANDCHILD, HOLDS_THE_PIPES, str(pid_file)],
                env=CLI_ENV,
                cwd=str(data.home),
            )

        assert spawned[0][3] == cli_sandbox.LAUNCHER
        assert "timed out" in caught.value.message
        assert pid_file.exists() and pid_file.read_text(), "the grandchild did not start"
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _is_running(pid):
            await asyncio.sleep(0.05)
        assert not _is_running(pid), "the grandchild outlived the timeout"
    finally:
        if pid_file.exists() and pid_file.read_text().strip():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except OSError:
                pass  # gone, as it should be
