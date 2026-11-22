"""Whether a provider CLI starts under the Landlock launcher, and what the controller does where it cannot (N1 and
its decision 5): app/cli_sandbox.py, and run_cli, the one place that starts a CLI.

The decisions are tested anywhere, with the probe stood in for. What a CLI that run_cli started can then open is
tested on Linux where Landlock works, as in test_cli_sandbox_launcher.py.
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import errno
import importlib.machinery
import json
import logging
import os
import posixpath
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import cli_sandbox
from app.cli_sandbox_launcher import EXIT_NOT_APPLIED, MARKER
from app.config import Settings
from app.process_hardening import PRIVATE_TEMP_NAME
from app.providers.base import CLIResult, ProviderAPIError
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter
from tests._codex_mcp_list import answers_mcp_list
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
# The settings of the controller's stores, each checked as a secret's is (N1-C-8), by where each is kept in /data.
STORE_SETTINGS = {
    "APPROVAL_ROOT": "approvals",
    "AUDIT_ROOT": "audit",
    "STATE_DB_PATH": "state.db",
    "SESSION_STORE_ROOT": "sessions",
    "JOB_STORE_ROOT": "jobs",
    "MEMORY_ROOT": "memory",
    "HARNESS_ROOT": "harness",
    "MESH_PEERS_PATH": "mesh/peers.json",
    "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
    "CRON_STORE_PATH": "crons/crons.json",
    "SHARE_REVOCATION_STORE_PATH": "share/revocations.json",
    "ISOLATED_TUNNEL_KNOWN_HOSTS_PATH": "ssh/known_hosts",
    "ISOLATED_TUNNEL_INFO_ROOT": "tunnels/sessions",
    "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
    "ARTIFACT_ROOT": "artifacts",
    "UPLOAD_ROOT": "uploads",
    "COMPLIANCE_MANIFEST_PATH": "compliance-manifest.json",
}
COMMAND = ["cli", "--flag"]
CLI_ENV = {"PATH": os.environ.get("PATH", ""), "PROVIDER_KEY": "value"}
# site's own, before a fixture stands in for it: it reads PYTHONUSERBASE as it is, where site.USER_BASE is unset.
_SITE_GETUSERBASE = site.getuserbase
_DECISION = '{"action":"done","reason":"x","risk_category":"read"}'


@pytest.fixture(autouse=True)
def _a_process_that_has_probed_and_reported_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe's answer, and each line already written, are kept for the process: every test starts without."""
    monkeypatch.setattr(cli_sandbox, "_asked", None)
    monkeypatch.setattr(cli_sandbox, "_reported", set())

    # The controller's own temp directory where its lifespan makes it (use_private_temp_directory), in whatever root
    # the test has: the tests about it set the real one.
    def in_the_root() -> str:
        return cli_sandbox.os.path.join(cli_sandbox.PROTECTED_ROOT, PRIVATE_TEMP_NAME)

    monkeypatch.setattr(cli_sandbox, "tempfile", SimpleNamespace(gettempdir=in_the_root))


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
    # The controller's own home, outside every directory a CLI is given unless a test puts it there: as HOME names
    # it, as its passwd entry does, and with Python's user base in it.
    controller_home = root.parent / "homes" / "controller"
    controller_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(controller_home))
    monkeypatch.setenv("USERPROFILE", str(controller_home))
    monkeypatch.setattr(cli_sandbox, "_passwd_home", lambda: str(controller_home))
    monkeypatch.setattr(site, "getuserbase", lambda: str(controller_home / ".local"))
    return SimpleNamespace(
        root=root,
        home=home,
        shot=shots[0],
        other_shot=shots[1],
        outside=root.parent / "outside",
        controller_home=controller_home,
    )


def _settings(data: SimpleNamespace, **changed: str | None) -> Settings:
    """Settings that keep every secret and store in the protected root, but for what ``changed`` says."""
    inside = {
        "API_TOKEN_FILE": "api-token",
        "TAKEOVER_TOKEN_FILE": "takeover-token",
        "AUTH_ROOT": "auth",
        "WITNESS_ROOT": "witness",
        "MESH_IDENTITY_DIR": "mesh/identity",
        "ISOLATED_TUNNEL_KEY_PATH": "ssh/id_ed25519",
        "PROXY_PERSONA_FILE": "proxy-personas.json",
        "BROWSER_WS_ENDPOINT_FILE": "browser-profile/browser-ws-endpoint.txt",
        **STORE_SETTINGS,
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


def test_the_store_settings_are_each_a_setting_by_that_name_kept_in_the_protected_root_by_default() -> None:
    assert sorted(cli_sandbox.STORE_PATH_SETTINGS.values()) == sorted(STORE_SETTINGS)
    assert not set(cli_sandbox.STORE_PATH_SETTINGS) & set(cli_sandbox.SECRET_PATH_SETTINGS)
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)

    for attribute, name in cli_sandbox.STORE_PATH_SETTINGS.items():
        assert Settings.model_fields[attribute].alias == name, f"{name} does not set Settings.{attribute}"
        default = getattr(settings, attribute)
        if name == "STATE_DB_PATH":
            assert default is None, "the one that is unset by default"
            continue
        assert PurePosixPath(default) == PurePosixPath(cli_sandbox.PROTECTED_ROOT, STORE_SETTINGS[name]), name


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


@pytest.mark.parametrize("setting", list(STORE_SETTINGS))
async def test_a_store_outside_the_root_is_named_as_a_secret_is_and_required_refuses(
    data, monkeypatch, caplog, setting: str
) -> None:
    # What a store holds (the approvals' digest key, a session, a job, a peer) a CLI is not to read or change, any
    # more than a secret: kept outside the root, the ruleset leaves it open.
    _probe_says(monkeypatch, None)
    outside = {setting: str(data.outside / "kept-elsewhere")}

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, **outside), data)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **outside), data)

    assert command == _under_the_launcher(data), "it still runs, and under the launcher"
    (line,) = _written(caplog)
    assert [name for name in [*SECRET_SETTINGS, *STORE_SETTINGS] if name in line] == [setting]
    assert "kept-elsewhere" not in line and str(data.outside) not in line, "a name, never a value"


@pytest.mark.parametrize("name, setting", [("browser-sessions", "ARTIFACT_ROOT"), ("witness", "AUDIT_ROOT")])
async def test_a_store_no_setting_names_is_named_by_the_setting_it_lies_beside_and_required_refuses(
    data, monkeypatch, caplog, name: str, setting: str
) -> None:
    # B5-fix1 item 3 (C-3, OC-8): the isolated sessions' profiles and the endpoint files the controller trusts lie in
    # browser-sessions beside ARTIFACT_ROOT's place, and the witness keys in witness beside AUDIT_ROOT's where
    # WITNESS_ROOT cannot be made; no setting names either.
    _probe_says(monkeypatch, None)
    (granted,) = _directories(data.root / name)
    changed = {"CLI_SANDBOX_WRITABLE": granted, "WITNESS_ROOT": str(data.root / "keys")}

    lines, refused = await _warned_and_refused(data, caplog, changed)

    assert len(lines) == 1 and f"{name} beside {setting}" in lines[0] and refused, lines


def test_the_isolated_sessions_runtime_root_is_where_the_check_looks_for_it(data) -> None:
    from app.session_isolation import DockerBrowserNodeProvisioner

    provisioner = DockerBrowserNodeProvisioner.__new__(DockerBrowserNodeProvisioner)
    provisioner.settings = _settings(data)

    found = cli_sandbox._derived_store_places(provisioner.settings)["browser-sessions beside ARTIFACT_ROOT"]
    assert provisioner._local_runtime_root("session-1").parent == Path(found)


def test_the_witness_keys_place_where_witness_root_cannot_be_made_is_where_the_check_looks_for_it(data) -> None:
    # B5-fix2 item 12 (opus-s2's note): BrowserManager keeps the witness keys beside AUDIT_ROOT's place where
    # WITNESS_ROOT cannot be made; the check looks for them there (DERIVED_STORE_PLACES).
    from app.browser_manager import BrowserManager

    blocker = data.root / "not-a-directory"
    blocker.write_text("a file, so WITNESS_ROOT cannot be made in it", encoding="utf-8")
    settings = _settings(data, WITNESS_ROOT=str(blocker / "witness"))

    found = cli_sandbox._derived_store_places(settings)["witness beside AUDIT_ROOT"]
    assert Path(BrowserManager(settings).settings.witness_root) == Path(found)


@pytest.mark.parametrize("where", ["in the cli home", "outside the root", "outside the root, with a '..' part"])
async def test_the_host_bridges_socket_is_named_where_a_cli_may_put_its_own_in_its_place(
    data, monkeypatch, caplog, where: str
) -> None:
    # The controller sends Codex's calls to OPENAI_HOST_BRIDGE_SOCKET and takes what answers there as Codex. A CLI
    # connects to a Unix socket wherever it is (Landlock has no right for a connect), so a socket outside the root is
    # no more open than one in it; but where the CLI may write, it can bind a socket of its own in the bridge's place.
    _probe_says(monkeypatch, None)
    (bridge,) = _directories(data.outside / "host-bridge")
    socket_path = {
        "in the cli home": str(data.home / "codex.sock"),
        "outside the root": str(Path(bridge) / "codex.sock"),
        "outside the root, with a '..' part": str(Path(bridge) / ".." / "host-bridge" / "codex.sock"),
    }[where]
    changed = {"OPENAI_HOST_BRIDGE_SOCKET": socket_path}

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, **changed), data)

    if where == "outside the root":
        assert await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data)
        assert _written(caplog) == _written(caplog, logging.ERROR) == []
        return
    (line,) = _written(caplog)
    assert "OPENAI_HOST_BRIDGE_SOCKET" in line and socket_path not in line
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data)


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


async def test_the_shared_directories_that_are_there_are_given_each_as_its_resolved_path(
    data, monkeypatch, caplog
) -> None:
    # One that is not there (a host with no /var/tmp) is passed over, and nothing is said of it.
    _probe_says(monkeypatch, None)
    temp, devices, real_var_temp = _directories(
        data.outside / "tmp", data.outside / "dev", data.outside / "real-var-tmp"
    )
    _link_to_directory(data.outside / "var-tmp", Path(real_var_temp))
    shared = (temp, str(data.outside / "var-tmp"), str(data.outside / "not-there"), devices)
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", shared)

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data), data)

    assert _given_to_write(command) == [str(data.home), temp, real_var_temp, devices]
    assert _written(caplog) == []


async def test_a_temp_directory_of_the_clis_that_names_a_shared_directory_not_there_is_said(
    data, monkeypatch, caplog
) -> None:
    # B5-fix1 item 15 (OS-1): the shared directory that is not there is passed over in silence, but a TMPDIR that
    # names it is said as one that names any other directory not there is; the silence keyed on the text before.
    _probe_says(monkeypatch, None)
    not_there = str(data.outside / "var-tmp")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (not_there,))

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data), data, env={**CLI_ENV, "TMPDIR": not_there})

    (line,) = _written(caplog)
    assert line.startswith(f"the CLI's TMPDIR names {not_there!r}, which is not a directory")


async def test_a_shared_directory_that_resolves_into_the_root_is_not_given_and_that_is_said(
    data, monkeypatch, caplog
) -> None:
    # A /tmp linked into /data, say: the CLI has no temp directory to write there, and the operator is told why.
    _probe_says(monkeypatch, None)
    inside, _ = _directories(data.root / "tmp", data.outside)
    _link_to_directory(data.outside / "tmp", Path(inside))
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (str(data.outside / "tmp"),))

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true"), data)

    assert _given_to_write(command) == [str(data.home)]
    (line,) = _written(caplog)
    assert line == (
        f"the shared directory {repr(str(data.outside / 'tmp'))}, which resolves to "
        f"{repr(inside)}, in {data.root}: provider CLIs are not given it to write"
    )


@pytest.mark.parametrize("variable", ["TMPDIR", "TEMP", "TMP"])
@pytest.mark.parametrize("where", ["outside the root", "in the root outside the home", "inside the home"])
async def test_a_temp_directory_of_the_clis_environment_is_given_outside_the_root_and_once(
    data, monkeypatch, caplog, variable: str, where: str
) -> None:
    # One under the root outside the home stays closed, as it always was, and that is said (it refuses nothing); one
    # inside the home is the home's.
    _probe_says(monkeypatch, None)
    place = {
        "outside the root": data.outside / "temp",
        "in the root outside the home": data.root / "temp",
        "inside the home": data.home / "temp",
    }[where]
    _directories(place)

    with caplog.at_level(logging.WARNING):
        command = await _launch(
            _settings(data, CLI_SANDBOX_REQUIRED="true"), data, env={**CLI_ENV, variable: str(place)}
        )

    expected = [str(data.home), str(place)] if where == "outside the root" else [str(data.home)]
    assert _given_to_write(command) == expected
    if where != "in the root outside the home":
        assert _written(caplog) == []
        return
    (line,) = _written(caplog)
    assert line == (
        f"the CLI's {variable} names {repr(str(place))}, which resolves to {repr(str(place))}, "
        f"in {data.root}: provider CLIs are not given it to write"
    )


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
    assert "CLI_SANDBOX_WRITABLE" in line and repr(str(missing)) in line


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


async def test_a_directory_a_cli_may_write_inside_a_secrets_directory_leaves_that_secret_open(
    data, monkeypatch, caplog
) -> None:
    # AUTH_ROOT is a directory, and what a CLI may write anywhere in it is the auth root's to read.
    _probe_says(monkeypatch, None)
    changed = {"CLI_SANDBOX_WRITABLE": _directories(data.root / "auth" / "sub")[0]}

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, **changed), data)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data)

    assert _given_to_write(command) == [str(data.home), changed["CLI_SANDBOX_WRITABLE"]]
    (line,) = _written(caplog)
    assert "AUTH_ROOT" in line and "WITNESS_ROOT" not in line
    assert "which lies outside %s, in a directory the CLIs may write, or holds one" % data.root in line


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


async def test_a_temp_directory_passed_over_for_a_home_that_a_later_entry_takes_the_place_of_is_given(
    data, monkeypatch, caplog
) -> None:
    # HOME is a link in a later entry of CLI_SANDBOX_WRITABLE, and TMPDIR lies where it leads. The entry takes
    # HOME's place, and covers where HOME is written, not where it leads, which a CLI may swap: TMPDIR, which lies
    # in nothing kept, has a rule of its own (X8-V1).
    _probe_says(monkeypatch, None)
    entry, real, tmpdir = _directories(
        data.outside / "opt" / "z", data.outside / "srv" / "y", data.outside / "srv" / "y" / "tmp"
    )
    _link_to_directory(Path(entry) / "y", Path(real))
    env = {**CLI_ENV, "HOME": str(Path(entry) / "y"), "TMPDIR": tmpdir}

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, CLI_SANDBOX_WRITABLE=entry), data, env=env)

    assert _given_to_write(command) == [str(data.home), entry, tmpdir]
    (line,) = _written(caplog)
    assert line.startswith(f"the CLI's HOME names {repr(str(Path(entry) / 'y'))}, which resolves to")


async def test_an_entry_passed_over_for_one_that_a_later_entry_takes_the_place_of_is_given(data, monkeypatch) -> None:
    # /opt/link leads to /srv/t, so /srv/t/sub lies in it; /opt then takes /opt/link's place, and covers where the
    # link is written, not where it leads: /srv/t/sub, which lies in nothing kept, has a rule of its own (C1-2).
    _probe_says(monkeypatch, None)
    opt, real, sub = _directories(data.outside / "opt", data.outside / "srv" / "t", data.outside / "srv" / "t" / "sub")
    _link_to_directory(Path(opt) / "link", Path(real))
    entries = ",".join([str(Path(opt) / "link"), sub, opt])

    command = await _launch(_settings(data, CLI_SANDBOX_WRITABLE=entries), data)

    assert _given_to_write(command) == [str(data.home), opt, sub]


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


async def _warned_and_refused(data, caplog, changed: dict[str, str], **launch) -> tuple[list[str], bool]:
    """The warnings of a call with these settings, and whether CLI_SANDBOX_REQUIRED=true refuses it."""
    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, **changed), data, **launch)
    try:
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data, **launch)
    except cli_sandbox.SandboxRequired:
        return _written(caplog), True
    return _written(caplog), False


@pytest.mark.parametrize(
    "place",
    ["the system's", "in an entry of CLI_SANDBOX_WRITABLE", "holds the CLI home", "outside the root, given to none"],
)
async def test_the_controllers_own_temp_directory_where_a_cli_can_reach_it_is_named_and_refused_where_required(
    data, monkeypatch, caplog, place: str
) -> None:
    # CLI-shared-temp-dirs: where the controller could not make its own (no root at its start, say), its temp files
    # are in the system's, which a CLI may read and write: an OCR's screenshot, a local browser's profile.
    _probe_says(monkeypatch, None)
    (system,) = _directories(data.outside / "tmp")
    changed: dict[str, str] = {}
    if place == "the system's":
        temp = system
        monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (system,))
    elif place == "in an entry of CLI_SANDBOX_WRITABLE":
        (entry,) = _directories(data.root / "scratch")
        temp = _directories(Path(entry) / "tmp")[0]
        changed = {"CLI_SANDBOX_WRITABLE": entry}
    elif place == "holds the CLI home":
        temp = str(data.root)
    else:
        # B5-fix1 item 17: a CLI under the sandbox reads outside PROTECTED_ROOT, given to it or not (M8 took the
        # place's root check off, and no case noticed).
        temp = _directories(data.outside / "srv-tmp")[0]
    monkeypatch.setattr(cli_sandbox, "tempfile", tempfile)
    monkeypatch.setattr(tempfile, "tempdir", temp)

    lines, refused = await _warned_and_refused(data, caplog, changed)

    assert len(lines) == 1 and "own temp files" in lines[0] and repr(temp) in lines[0], lines
    assert refused


async def test_the_controllers_own_temp_directory_in_the_root_is_not_named(data, monkeypatch, caplog) -> None:
    _probe_says(monkeypatch, None)
    (private,) = _directories(data.root / PRIVATE_TEMP_NAME)
    monkeypatch.setattr(cli_sandbox, "tempfile", tempfile)
    monkeypatch.setattr(tempfile, "tempdir", private)

    lines, refused = await _warned_and_refused(data, caplog, {})

    assert (lines, refused) == ([], False)


@pytest.mark.parametrize("temp", [False, True], ids=["no temp directory", "a temp directory holds the home's target"])
async def test_a_secret_written_through_a_cli_home_a_temp_directory_takes_the_place_of_is_open(
    data, monkeypatch, caplog, temp: bool
) -> None:
    # The home is a link into a temp directory, which then takes its place among the directories given: the secret,
    # written through the home's link, leads into that temp directory, where a CLI can swap what lies past the link.
    _probe_says(monkeypatch, None)
    (tmp,) = _directories(data.outside / "tmp")
    real_home = Path(_directories(Path(tmp) / "cli-home")[0])
    home = data.outside / "cli-home"
    _link_to_directory(home, real_home)
    (data.root / "auth").mkdir()
    _link_to_directory(real_home / "auth", data.root / "auth")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (tmp,) if temp else ())
    changed = {"CLI_HOME": str(home), "AUTH_ROOT": str(home / "auth")}

    lines, refused = await _warned_and_refused(data, caplog, changed, home=home)

    assert len(lines) == 1 and "AUTH_ROOT" in lines[0] and refused


@pytest.mark.parametrize("temp", [False, True], ids=["no temp directory", "a temp directory holds the entry's target"])
async def test_a_secret_written_through_an_entry_that_lies_in_a_temp_directory_once_resolved_is_open(
    data, monkeypatch, caplog, temp: bool
) -> None:
    # The entry of CLI_SANDBOX_WRITABLE is a link into a temp directory, and gets no rule of its own beside it.
    _probe_says(monkeypatch, None)
    (tmp,) = _directories(data.outside / "tmp")
    real_state = Path(_directories(Path(tmp) / "state")[0])
    state = data.outside / "state"
    _link_to_directory(state, real_state)
    (data.root / "witness").mkdir()
    _link_to_directory(real_state / "witness", data.root / "witness")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (tmp,) if temp else ())
    changed = {"CLI_SANDBOX_WRITABLE": str(state), "WITNESS_ROOT": str(state / "witness")}

    lines, refused = await _warned_and_refused(data, caplog, changed)

    assert len(lines) == 1 and "WITNESS_ROOT" in lines[0] and refused


async def test_an_entry_in_the_cli_home_that_leads_to_a_secret_of_the_root_leaves_that_secret_closed(
    data, monkeypatch, caplog
) -> None:
    # The entry lies in the home as written, so it has no rule of its own: nothing a CLI is given reaches where it
    # leads, and the controller reads AUTH_ROOT at its own path, not through the link. The entry is said not to be
    # given, as one that is not a directory is, and no secret is said to be open.
    _probe_says(monkeypatch, None)
    (auth,) = _directories(data.root / "auth")
    _link_to_directory(data.home / "x", Path(auth))
    changed = {"CLI_SANDBOX_WRITABLE": str(data.home / "x")}  # AUTH_ROOT stays data/auth

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, **changed), data)
    lines, refused = await _warned_and_refused(data, caplog, changed)

    assert _given_to_write(command) == [str(data.home)]
    assert [("not given it to write" in line, "AUTH_ROOT" in line) for line in lines] == [(True, False)]
    assert not refused


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


def _readlink_from(links: dict[str, str]):
    """os.readlink over ``links``, each a link's path to its target: any other path is no link, and Linux answers
    EINVAL."""

    def readlink(path: str) -> str:
        if path not in links:
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), path)
        return links[path]

    return readlink


# The image's PATH (measured): uvicorn's and the CLIs' own directory first, then python:3.11-slim's.
_IMAGE_PATH = "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _linux_os(paths: SimpleNamespace, links: dict[str, str], getcwd: Callable[[], str]) -> SimpleNamespace:
    """app.cli_sandbox's os on Linux: these paths, links read from ``links``, the working directory ``getcwd``
    answers, and the image's PATH in its environment, searched as os.get_exec_path searches it (os.defpath where PATH
    is not set)."""
    environ = {"PATH": _IMAGE_PATH}
    return SimpleNamespace(
        path=paths,
        readlink=_readlink_from(links),
        getcwd=getcwd,
        fspath=os.fspath,
        environ=environ,
        get_exec_path=lambda: environ.get("PATH", posixpath.defpath).split(":"),
    )


@pytest.fixture
def linux_paths(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """app.cli_sandbox's paths as Linux has them, on any host: posixpath and PurePosixPath, /data the root and /tmp
    the one shared directory, every path a directory, and realpath answered from the links a test names (as the
    triage's probe_slashes had them). Linux reads two leading slashes as one."""
    links: dict[str, str] = {}

    def realpath(path: str) -> str:
        resolved = posixpath.normpath("/" + str(path).lstrip("/"))
        for link, target in links.items():
            if resolved == link or resolved.startswith(link + "/"):
                return target + resolved[len(link) :]
        return resolved

    paths = SimpleNamespace(
        abspath=posixpath.abspath,
        realpath=realpath,
        isabs=posixpath.isabs,
        isdir=lambda path: True,
        join=posixpath.join,
        normpath=posixpath.normpath,
        expanduser=lambda path: "/home/controller",
    )
    monkeypatch.setattr(cli_sandbox, "os", _linux_os(paths, links, os.getcwd))  # posixpath.abspath's
    monkeypatch.setattr(cli_sandbox, "Path", PurePosixPath)
    monkeypatch.setattr(cli_sandbox, "PROTECTED_ROOT", "/data")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", ("/tmp",))
    monkeypatch.setattr(cli_sandbox, "_passwd_home", lambda: "/home/controller")
    monkeypatch.setattr(site, "getuserbase", lambda: "/home/controller/.local")
    return links


def _linux_settings(**values: str | None) -> Settings:
    """Settings read as on Linux, where /home/x is absolute (Windows takes it for none from Python 3.13 on). With a
    HARNESS_UV_COMMAND, HARNESS_VERIFIER is uv, which runs it, unless the test sets it (None: unset)."""
    if "HARNESS_UV_COMMAND" in values:
        values.setdefault("HARNESS_VERIFIER", "uv")
    set_values = {name: value for name, value in values.items() if value is not None}
    with patch.dict(os.environ, {}, clear=True), patch.object(os.path, "isabs", posixpath.isabs):
        return Settings(_env_file=None, **set_values)


def test_a_secret_written_with_two_slashes_in_the_cli_home_is_open_wherever_it_resolves(linux_paths) -> None:
    linux_paths["/data/cli-home/token"] = "/data/api-token"

    home = ("/data/cli-home",) * 2
    names = cli_sandbox._open_settings(
        _linux_settings(API_TOKEN_FILE="//data/cli-home/token"), [(home, cli_sandbox._places(home))]
    )

    assert names == ["API_TOKEN_FILE"]


def test_a_cli_home_written_with_two_slashes_in_tmp_and_swapped_for_a_link_is_tmps(linux_paths) -> None:
    linux_paths["/tmp/cli-home"] = "/home/controller/.local/lib/python3.12/site-packages"

    given, refused = cli_sandbox._writable(_linux_settings(), "//tmp/cli-home", {})

    # The CLI writes /tmp, and not where its home leads: it runs without a home it can write, and that is said.
    assert given == [("/tmp", "/tmp")]
    assert "CLI_HOME '//tmp/cli-home' resolves to '/home/controller/.local/lib/python3.12/site-packages'" in refused


def test_a_cli_home_written_with_two_slashes_in_the_root_is_given(linux_paths) -> None:
    given, refused = cli_sandbox._writable(_linux_settings(), "//data/cli-home", {})

    assert (given, refused) == ([("/data/cli-home",) * 2, ("/tmp", "/tmp")], "")


def test_an_entry_written_with_two_slashes_in_the_root_is_given(linux_paths, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        given, _ = cli_sandbox._writable(_linux_settings(CLI_SANDBOX_WRITABLE="//data/shared"), "/data/cli-home", {})

    assert given == [("/data/cli-home",) * 2, ("/tmp", "/tmp"), ("/data/shared",) * 2]
    assert _written(caplog) == []


async def test_a_cli_home_written_as_two_slashes_and_the_root_is_the_root(linux_paths, monkeypatch, caplog) -> None:
    _probe_says(monkeypatch, None)

    with caplog.at_level(logging.WARNING):
        command = await cli_sandbox.launch_command(
            _linux_settings(), COMMAND, cli_home="//data", image_path=None, env=CLI_ENV
        )

    assert command == COMMAND
    (line,) = _written(caplog)
    assert "CLI_HOME is /data or holds it" in line


@pytest.fixture
def linux_links(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """app.cli_sandbox on a Linux file system held in memory, on any host, whose realpath reads a path one part at a
    time and follows a link before the parts after it, a '..' among them, as Linux does (the triage's probe_sandbox).
    linux_paths normalises a path before it reads a link, as abspath does, so it cannot say where a '..' after a link
    leads; a junction on Windows cannot either, where a '..' is read off the text first. /data is the root, /tmp,
    /var/tmp and /dev the shared directories, /home/controller the controller's home as HOME and its passwd entry name
    it, with Python's user base in it, and the controller runs from the image's places (_controller_runs_from). A test
    adds ``dirs`` (each with its parents) and ``links`` (each to its target), and sets ``cwd`` to None for a working
    directory that was removed: getcwd, and abspath of a relative path, then answer ENOENT, as Linux's do."""
    disk = SimpleNamespace(
        dirs={"/data/cli-home", "/tmp", "/var/tmp", "/dev", "/home/controller/.ssh", "/home/controller/.local"},
        links={},
        home="/home/controller",
        passwd_home="/home/controller",
        user_base="/home/controller/.local",
        cwd="/app",
    )

    def realpath(path: str) -> str:
        pending = [part for part in str(path).split("/") if part not in ("", ".")][::-1]
        resolved, followed = "/", 0
        while pending:
            part = pending.pop()
            if part == "..":
                resolved = posixpath.dirname(resolved)
                continue
            candidate = posixpath.join(resolved, part)
            if candidate not in disk.links:
                resolved = candidate
                continue
            followed += 1
            assert followed < 40, "a loop of links"
            target = disk.links[candidate]
            resolved = "/" if target.startswith("/") else resolved
            pending.extend([part for part in target.split("/") if part not in ("", ".")][::-1])
        return resolved

    def isdir(path: str) -> bool:
        real = realpath(path)
        return real == "/" or any(directory == real or directory.startswith(real + "/") for directory in disk.dirs)

    def getcwd() -> str:
        if disk.cwd is None:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return disk.cwd

    paths = SimpleNamespace(
        # posixpath.abspath's own, from this file system's working directory.
        abspath=lambda path: posixpath.normpath(path if posixpath.isabs(path) else posixpath.join(getcwd(), path)),
        realpath=realpath,
        isabs=posixpath.isabs,
        isdir=isdir,
        join=posixpath.join,
        normpath=posixpath.normpath,
        expanduser=lambda path: disk.home + path[1:] if path == "~" or path.startswith("~/") else path,
    )
    disk.os = _linux_os(paths, disk.links, getcwd)  # a test about PATH sets disk.os.environ's
    monkeypatch.setattr(cli_sandbox, "os", disk.os)
    monkeypatch.setattr(cli_sandbox, "Path", PurePosixPath)
    monkeypatch.setattr(cli_sandbox, "PROTECTED_ROOT", "/data")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", ("/tmp", "/var/tmp", "/dev"))
    monkeypatch.setattr(cli_sandbox, "_passwd_home", lambda: disk.passwd_home)
    monkeypatch.setattr(site, "getuserbase", lambda: disk.user_base)
    _controller_runs_from(monkeypatch)
    _probe_says(monkeypatch, None)
    return disk


# The image's import path under uvicorn, for its Python 3.11: the script's directory, the working directory uvicorn
# adds for its --app-dir, and the standard library's and the packages' places. Measured in the image, uvicorn puts the
# working directory first, as '' (B5-fix6 item 8, O6-12: Opus's py_paths.txt D); here it is '.', second. Either reads
# as the working directory, and the verdicts are the same: this file passes whole with '' first (fix6/v8).
_IMAGE_IMPORT_PATH = (
    "/usr/local/bin",
    ".",
    "/usr/local/lib/python311.zip",
    "/usr/local/lib/python3.11",
    "/usr/local/lib/python3.11/lib-dynload",
    "/usr/local/lib/python3.11/site-packages",
)


def _controller_runs_from(
    monkeypatch: pytest.MonkeyPatch,
    *,
    launcher: str = "/app/app/cli_sandbox_launcher.py",
    executable: str = "/usr/local/bin/python3",
    prefix: str = "/usr/local",
    base_prefix: str = "/usr/local",
    stdlib: str = "/usr/local/lib/python3.11",
    site_packages: str = "/usr/local/lib/python3.11/site-packages",
    import_path: tuple[str, ...] = _IMAGE_IMPORT_PATH,
    pycache_prefix: str | None = None,
) -> None:
    """Where the controller runs its code from, as the image has it unless a test says otherwise, and not where this
    test process does: its launcher in its package, its interpreter, its prefix and base prefix, its standard library,
    its packages, its import path and its bytecode prefix. The module's sys is a stand-in for it, so that no import of
    this process looks in a place of the image's."""
    installed = {"stdlib": stdlib, "platstdlib": stdlib, "purelib": site_packages, "platlib": site_packages}
    monkeypatch.setattr(cli_sandbox, "LAUNCHER", launcher)
    stand_in = SimpleNamespace(
        executable=executable,
        prefix=prefix,
        base_prefix=base_prefix,
        path=list(import_path),
        pycache_prefix=pycache_prefix,
    )
    monkeypatch.setattr(cli_sandbox, "sys", stand_in)
    monkeypatch.setattr(sysconfig, "get_paths", lambda *args, **kwargs: installed)


async def _launch_on_linux(settings: Settings, cli_home: str, env: dict[str, str]) -> list[str]:
    return await cli_sandbox.launch_command(settings, COMMAND, cli_home=cli_home, image_path=None, env=env)


async def _reads_on_linux(
    caplog: pytest.LogCaptureFixture,
    named: str | None,
    home: str = "/data/cli-home",
    clause: str = "and the controller reads %r",
    **changed: str,
) -> list[str]:
    """A call with the CLI home ``home``, then one under CLI_SANDBOX_REQUIRED=true: where ``named`` is None nothing is
    said and the CLI runs; otherwise one line names ``named`` in ``clause`` (what the controller reads, by default;
    a row about the controller's own home gives that clause), and the second call is refused. The first call's
    command."""
    env = {"HOME": home}
    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(**changed), home, env)
    required = _linux_settings(CLI_SANDBOX_REQUIRED="true", **changed)
    if named is None:
        assert _written(caplog) == []
        assert await _launch_on_linux(required, home, env)
        return command
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(required, home, env)
    (line,) = _written(caplog)
    # B5-fix3 item 9 (O3-7): the clause the row expects, which the old tests pinned, not the name anywhere after
    # "under the Landlock sandbox, ".
    assert clause % named in line, line
    return command


async def test_a_place_read_and_a_directory_given_that_are_two_links_to_one_directory_are_reported(
    linux_links, monkeypatch, caplog
) -> None:
    # B5-fix4 item 8 (M4-N5b): /opt/lib-link, an entry of the import path, and /srv/given-link, given, both lead to
    # /srv/shared/lib. Neither is, holds or lies in the other as written, nor at any of its steps: only their real
    # paths meet, so the real path is one of the spellings _places compares (the orchestrator's fuzz found 360 such
    # pairs that dropping it hides).
    linux_links.links.update({"/opt/lib-link": "/srv/shared/lib", "/srv/given-link": "/srv/shared/lib"})
    linux_links.dirs.add("/srv/shared/lib")
    import_path = (*(entry for entry in _IMAGE_IMPORT_PATH if entry != "."), "/opt/lib-link")
    _controller_runs_from(monkeypatch, import_path=import_path)

    await _reads_on_linux(caplog, "/opt/lib-link", CLI_SANDBOX_WRITABLE="/srv/given-link")


# A place in /srv/tools past quoted_for_log's cut (MAX_LOGGED_PATH_CHARS, 200 characters).
_PAST_THE_CUT = "/srv/tools/" + "p" * 120 + "/" + "q" * 100


@pytest.mark.parametrize(
    "what, clause",
    [("import path", "and the controller reads %r"), ("HOME", "and the controller's own home %r")],
    ids=["an entry of the import path, in the line about what the controller reads", "the line about its own home"],
)
async def test_a_place_past_200_characters_is_named_whole(
    linux_links, monkeypatch, caplog, what: str, clause: str
) -> None:
    # B5-fix4 item 8 (M4-N20): a CLI names what it makes, and could pad a name past quoted_for_log's cut to hide where
    # a link it swapped in leads (B5-fix3 item 7, C3-9): each line names a place whole.
    linux_links.dirs.update({"/srv/tools", _PAST_THE_CUT})
    import_path = tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != ".")
    if what == "HOME":
        linux_links.home = _PAST_THE_CUT
    else:
        import_path += (_PAST_THE_CUT,)
    _controller_runs_from(monkeypatch, import_path=import_path)

    await _reads_on_linux(caplog, _PAST_THE_CUT, clause=clause, CLI_SANDBOX_WRITABLE="/srv/tools")


# A link a CLI can make in /tmp, to a directory the controller runs code from, and a '..' after it.
_SLOT_LINK = {"/tmp/slot": "/opt/venv/lib/python3.11"}
_CLIMBS_PAST_THE_SLOT = "/tmp/slot/../../lib/python3.11/site-packages"


@pytest.mark.parametrize("dots", [True, False], ids=["with a '..' after the link", "the link alone"])
async def test_a_temp_directory_whose_path_climbs_back_out_of_a_link_is_not_given(
    linux_links, caplog, dots: bool
) -> None:
    # Text first, /tmp/slot/../.. is /, and the path lies in no directory given; Linux follows the link first, and a
    # CLI that made it chooses which directory beside its target is given. The CLI's TMPDIR is then no directory it
    # may write, and that is said.
    linux_links.dirs.add("/opt/venv/lib/python3.11/site-packages")
    linux_links.links.update(_SLOT_LINK)
    tmpdir = _CLIMBS_PAST_THE_SLOT if dots else "/tmp/slot/site-packages"
    env = {"HOME": "/data/cli-home", "TMPDIR": tmpdir}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(CLI_SANDBOX_REQUIRED="true"), "/data/cli-home", env)

    assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]
    why = "has a '..' part" if dots else "resolves to '/opt/venv/lib/python3.11/site-packages', which no directory"
    (line,) = _written(caplog)
    assert line.startswith(f"the CLI's TMPDIR names {tmpdir!r}, which {why}")


async def test_an_entry_of_cli_sandbox_writable_whose_path_climbs_out_of_a_link_is_refused_and_not_given(
    linux_links, caplog
) -> None:
    linux_links.dirs.add("/opt/venv/lib/python3.11/site-packages")
    linux_links.links.update(_SLOT_LINK)
    with pytest.raises(ValueError, match=r"CLI_SANDBOX_WRITABLE.*'\.\.'"):
        _linux_settings(CLI_SANDBOX_WRITABLE=_CLIMBS_PAST_THE_SLOT)
    # Read past the setting's own check (a Settings made without it): the sandbox does not give it either.
    settings = SimpleNamespace(cli_sandbox_writable_paths=[_CLIMBS_PAST_THE_SLOT])

    with caplog.at_level(logging.WARNING):
        given, refused = cli_sandbox._writable(settings, "/data/cli-home", {"HOME": "/data/cli-home"})

    assert ([real for _, real in given], refused) == (["/data/cli-home", "/tmp", "/var/tmp", "/dev"], "")
    (line,) = _written(caplog)
    assert _CLIMBS_PAST_THE_SLOT in line and "'..'" in line


@pytest.mark.parametrize(
    "home, link",
    [(_CLIMBS_PAST_THE_SLOT, _SLOT_LINK), ("/data/link/..", {"/data/link": "/tmp/foo"})],
    ids=["through a link a CLI can make", "through an operator's link in the root"],
)
async def test_a_cli_home_whose_path_climbs_out_of_a_link_is_not_given_and_the_cli_starts_under_the_launcher(
    linux_links, caplog, home: str, link: dict[str, str]
) -> None:
    # As text, /data/link/.. is /data, and a CLI_HOME that is /data starts the CLI without the launcher: Linux reads
    # /tmp/foo/.. there, which is /tmp.
    linux_links.dirs |= {"/opt/venv/lib/python3.11/site-packages", "/tmp/foo"}
    linux_links.links.update(link)
    env = {"HOME": home}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(), home, env)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(_linux_settings(CLI_SANDBOX_REQUIRED="true"), home, env)

    assert _given_to_write(command) == ["/tmp", "/var/tmp", "/dev"]
    (line,) = _written(caplog)
    assert f"CLI_HOME {home!r} has a '..' part" in line


@pytest.mark.parametrize(
    "home, entry",
    [("/tmp/work/..", ""), ("/tmp/h", "/data/w")],
    ids=["a '..' that leads to /tmp", "a link to a directory CLI_SANDBOX_WRITABLE names in the root"],
)
async def test_a_refused_cli_home_that_another_directory_given_holds_once_resolved_is_said_to_have_no_rule_of_its_own(
    linux_links, caplog, home: str, entry: str
) -> None:
    # The home has no rule, and the CLI still writes it, through the rule of the directory that holds where it leads:
    # the line says the first and not that the CLI cannot write its home.
    linux_links.dirs |= {"/tmp/work", "/data/w"}
    linux_links.links["/tmp/h"] = "/data/w"
    env = {"HOME": home}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(CLI_SANDBOX_WRITABLE=entry), home, env)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(_linux_settings(CLI_SANDBOX_WRITABLE=entry, CLI_SANDBOX_REQUIRED="true"), home, env)

    assert _given_to_write(command) == ["/tmp", "/var/tmp", "/dev", *([entry] if entry else [])]
    (line,) = _written(caplog)
    assert f"CLI_HOME {home!r} " in line and "run under the Landlock sandbox with no rule for their home (" in line


async def test_a_cli_home_with_no_dots_in_the_root_is_given(linux_links, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(), "/data/cli-home", {"HOME": "/data/cli-home"})

    assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]
    assert _written(caplog) == []


@pytest.mark.parametrize("dots", [True, False], ids=["with a '..' after a link", "the same place, written without"])
async def test_a_secret_whose_path_has_dots_after_a_link_is_named_open_and_required_refuses(
    linux_links, caplog, dots: bool
) -> None:
    # The CLI writes its home and can make /data/cli-home/slot a link to any directory, and Linux reads the '..' after
    # it: AUTH_ROOT leads to /data/auth while the link is this one, and wherever the CLI points it after.
    linux_links.dirs |= {"/data/x/y", "/data/auth"}
    linux_links.links["/data/cli-home/slot"] = "/data/x/y"
    auth_root = "/data/cli-home/slot/../../auth" if dots else "/data/auth"
    env = {"HOME": "/data/cli-home"}
    required = _linux_settings(AUTH_ROOT=auth_root, CLI_SANDBOX_REQUIRED="true")

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(AUTH_ROOT=auth_root), "/data/cli-home", env)
    if dots:
        with pytest.raises(cli_sandbox.SandboxRequired):
            await _launch_on_linux(required, "/data/cli-home", env)
    else:
        assert await _launch_on_linux(required, "/data/cli-home", env) == command

    assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]
    # The setting by its name, never its value.
    lines = _written(caplog)
    assert [("AUTH_ROOT" in line, "'..'" in line, auth_root in line) for line in lines] == (
        [(True, True, False)] if dots else []
    )


async def test_a_cli_home_in_tmp_that_is_a_link_elsewhere_is_said_to_be_refused_and_required_refuses(
    linux_links, caplog
) -> None:
    # /tmp's rule covers the home's place, and not where the link leads, which a CLI may swap: the CLI runs with no
    # home it can write, as where the home resolves into the root.
    linux_links.dirs.add("/opt/cli-state")
    linux_links.links["/tmp/cli-home"] = "/opt/cli-state"
    env = {"HOME": "/tmp/cli-home"}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(), "/tmp/cli-home", env)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(_linux_settings(CLI_SANDBOX_REQUIRED="true"), "/tmp/cli-home", env)

    assert _given_to_write(command) == ["/tmp", "/var/tmp", "/dev"]
    (line,) = _written(caplog)
    assert "CLI_HOME '/tmp/cli-home' resolves to '/opt/cli-state'" in line


@pytest.mark.parametrize(
    "writable, tmpdir",
    [
        ("/srv/link", None),
        ("/srv/link/", None),
        ("/srv/link/.", None),
        ("/srv/link//", None),
        ("", "/srv/link/."),
    ],
    ids=["as the home is written", "a trailing '/'", "a trailing '/.'", "a trailing '//'", "the CLI's TMPDIR as '/.'"],
)
async def test_a_refused_entry_of_cli_sandbox_writable_that_is_the_cli_home_is_named_once_as_the_home(
    linux_links, caplog, writable: str, tmpdir: str | None
) -> None:
    # B5-fix2 item 8 (H-2, OC2-12): the home's own warning names it, as _writable's docstring and the CHANGELOG say,
    # and the entry's refusal said it a second time. B5-fix3 item 1 (O3-1 = C3-7): Linux reads the home the same with
    # a trailing '/', '/.' or '//', and so does the check.
    linux_links.links["/srv/link"] = "/data/cli-home"
    env = {"HOME": "/srv/link", **({"TMPDIR": tmpdir} if tmpdir else {})}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(CLI_SANDBOX_WRITABLE=writable), "/srv/link", env)

    assert _given_to_write(command) == ["/tmp", "/var/tmp", "/dev"]
    (line,) = _written(caplog)
    assert line.startswith("CLI_HOME '/srv/link' resolves to '/data/cli-home', in /data: "), line


async def test_a_long_place_a_link_leads_to_is_named_whole_and_each_place_is_a_line_of_its_own(
    linux_links, caplog
) -> None:
    # A CLI names what it makes: with each path cut to 200 characters for the log, a link it swapped in for the home
    # led to a padded place that the warning did not name, and a second such place, the same in its first 200
    # characters, made the same cut text, which was never written.
    padded = "/opt/" + "p" * 250
    for leads in ("first", "second"):
        linux_links.dirs.add(f"{padded}/{leads}")
        linux_links.links["/tmp/cli-home"] = f"{padded}/{leads}"
        with caplog.at_level(logging.WARNING):
            await _launch_on_linux(_linux_settings(), "/tmp/cli-home", {"HOME": "/tmp/cli-home"})

    first, second = _written(caplog)
    assert f"CLI_HOME '/tmp/cli-home' resolves to '{padded}/first'" in first
    assert f"CLI_HOME '/tmp/cli-home' resolves to '{padded}/second'" in second


@pytest.mark.parametrize("linked", [True, False], ids=["a link out of /tmp", "a directory in /tmp"])
async def test_an_entry_of_cli_sandbox_writable_in_tmp_that_is_a_link_elsewhere_is_said_not_to_be_given(
    linux_links, caplog, linked: bool
) -> None:
    # /tmp's rule covers where the entry is written, and not where the link leads, which a CLI may swap: the entry is
    # not given there, and that is said. Like an entry that is not a directory, it refuses nothing.
    if linked:
        linux_links.dirs.add("/opt/toolcache")
        linux_links.links["/tmp/out"] = "/opt/toolcache"
    else:
        linux_links.dirs.add("/tmp/out")
    env = {"HOME": "/data/cli-home"}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(CLI_SANDBOX_WRITABLE="/tmp/out"), "/data/cli-home", env)
        required = await _launch_on_linux(
            _linux_settings(CLI_SANDBOX_WRITABLE="/tmp/out", CLI_SANDBOX_REQUIRED="true"), "/data/cli-home", env
        )

    assert _given_to_write(command) == _given_to_write(required) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]
    lines = _written(caplog)
    if not linked:
        assert lines == []
        return
    (line,) = lines
    assert "CLI_SANDBOX_WRITABLE names '/tmp/out', which resolves to '/opt/toolcache'" in line
    assert "not given it to write" in line


@pytest.mark.parametrize(
    "links, given",
    [
        ({"/opt/x": "/tmp/sub/inner", "/tmp/sub/inner": "/target"}, False),
        ({"/opt/x": "../tmp/sub/inner", "/tmp/sub/inner": "/target"}, False),
        ({"/opt/x": "/data/cli-home/slot", "/data/cli-home/slot": "/target"}, False),
        ({"/opt/x": "/tmp/sub/../../target"}, False),
        ({"/opt/x": "/srv/inner", "/srv/inner": "/target"}, True),
    ],
    ids=[
        "to a link in /tmp",
        "to a link in /tmp, written relative",
        "to a link in the CLI home",
        "through a directory in /tmp, with a '..' after it",
        "through no directory a CLI may write",
    ],
)
async def test_an_entry_of_cli_sandbox_writable_that_resolves_through_a_place_a_cli_may_write_is_not_given(
    linux_links, caplog, links: dict[str, str], given: bool
) -> None:
    # An operator's link, outside every directory a CLI may write, to a place a CLI may change on the way to its
    # target: the CLI chooses where the entry leads, and so which directory the next call is given to write
    # (/tmp/sub/inner made a link to the controller's code, say). Neither the entry as written nor where it leads now
    # lies in /tmp; each place Linux looks up on the way does.
    linux_links.dirs |= {"/target", "/tmp/sub", "/srv"}
    linux_links.links.update(links)
    env = {"HOME": "/data/cli-home"}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(CLI_SANDBOX_WRITABLE="/opt/x"), "/data/cli-home", env)
        required = await _launch_on_linux(
            _linux_settings(CLI_SANDBOX_WRITABLE="/opt/x", CLI_SANDBOX_REQUIRED="true"), "/data/cli-home", env
        )

    expected = ["/data/cli-home", "/tmp", "/var/tmp", "/dev", *(["/target"] if given else [])]
    assert _given_to_write(command) == _given_to_write(required) == expected
    lines = _written(caplog)
    if given:
        assert lines == []
        return
    (line,) = lines
    assert "CLI_SANDBOX_WRITABLE names '/opt/x', which resolves to '/target', which no directory given" in line


async def test_a_cli_home_that_resolves_through_a_link_in_tmp_is_refused_and_required_refuses(
    linux_links, caplog
) -> None:
    # /tmp takes the home's place, as it does a home written in /tmp that is a link elsewhere.
    linux_links.dirs |= {"/srv/h", "/tmp/sub"}
    linux_links.links.update({"/opt/cli-home": "/tmp/sub/h", "/tmp/sub/h": "/srv/h"})
    env = {"HOME": "/opt/cli-home"}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(), "/opt/cli-home", env)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(_linux_settings(CLI_SANDBOX_REQUIRED="true"), "/opt/cli-home", env)

    assert _given_to_write(command) == ["/tmp", "/var/tmp", "/dev"]
    (line,) = _written(caplog)
    assert "CLI_HOME '/opt/cli-home' resolves to '/srv/h', which no directory given" in line


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link takes a privilege on Windows, and Landlock is Linux's")
async def test_an_entry_that_resolves_through_a_link_in_a_temp_directory_is_not_given_where_the_link_leads(
    data, monkeypatch, caplog
) -> None:
    # The same on this host's file system: an operator's link outside the temp directory to a link in it.
    _probe_says(monkeypatch, None)
    (temp,) = _directories(data.outside / "tmp")
    monkeypatch.setattr(cli_sandbox, "SHARED_WRITABLE", (temp,))
    target = Path(_directories(data.outside / "target")[0])
    Path(temp, "sub").mkdir()
    Path(temp, "sub", "inner").symlink_to(target, target_is_directory=True)
    entry = data.outside / "opt" / "x"
    entry.parent.mkdir()
    entry.symlink_to(Path(temp, "sub", "inner"), target_is_directory=True)

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, CLI_SANDBOX_WRITABLE=str(entry)), data)

    assert _given_to_write(command) == [str(data.home), temp]
    (line,) = _written(caplog)
    assert (
        f"CLI_SANDBOX_WRITABLE names {str(entry)!r}, which resolves to {str(target)!r}, which no directory given"
        in line
    )


@pytest.mark.parametrize(
    "entry, home, link, target",
    [
        ("/tmp/out", "/data/cli-home", "/tmp/out", "/data/cli-home/x\nFORGED line"),
        ("/tmp/out", "/data/cli-home", "/tmp/out", "/opt/x\nFORGED line"),
        ("", "/tmp/cli-home", "/tmp/cli-home", "/opt/x\nFORGED line"),
        ("/srv/w", "/data/cli-home", "/srv/w", "/home/controller/x\nFORGED line"),
    ],
    ids=[
        "an entry that resolves into the root",
        "an entry that resolves where no directory given holds it",
        "a CLI home that does",
        "an entry that resolves into the controller's home",
    ],
)
async def test_a_path_with_a_line_break_in_it_is_quoted_and_starts_no_line_of_the_log(
    linux_links, caplog, entry: str, home: str, link: str, target: str
) -> None:
    # A CLI names what it makes where it may write, a line break and all, and can swap a link there for one to it:
    # where the sandbox names that path, it is quoted, its line break escaped, and forges no line of its own.
    linux_links.dirs.add(target)
    linux_links.links[link] = target

    with caplog.at_level(logging.WARNING):
        await _launch_on_linux(_linux_settings(CLI_SANDBOX_WRITABLE=entry), home, {"HOME": home})

    lines = [record.getMessage() for record in caplog.records if record.name == "app.cli_sandbox"]
    assert [line for line in lines if "FORGED" in line], "the path is named"
    assert [line for line in lines if "\n" in line] == []


@pytest.mark.skipif(os.name == "nt", reason="a name with a line break in it cannot be made on Windows")
async def test_a_name_with_a_line_break_that_an_entry_resolves_to_is_quoted_in_the_log(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)
    forged = data.root / "x\nFORGED line"
    forged.mkdir()
    entry = data.outside / "out"
    entry.parent.mkdir()
    entry.symlink_to(forged, target_is_directory=True)

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, CLI_SANDBOX_WRITABLE=str(entry)), data)

    (line,) = _written(caplog)
    assert "\n" not in line and repr(str(forged)) in line


@pytest.mark.parametrize(
    "home, home_variable",
    [
        ("/tmp/cli-home", "/tmp/cli-home"),
        ("/data/cli-home", "/data/cli-home"),
        ("/home/botuser", "/home/botuser"),
        ("/home/controller/.cache/auto-browser/cli-home", "/home/controller"),
    ],
    ids=["a directory in /tmp", "the image's", "a host user's home", "the private one of a blank CLI_HOME"],
)
def test_a_cli_home_that_a_directory_given_holds_once_resolved_is_not_refused(
    linux_links, home: str, home_variable: str
) -> None:
    linux_links.dirs |= {home, home_variable}

    given, refused = cli_sandbox._writable(_linux_settings(), home, {"HOME": home_variable})

    assert refused == ""
    assert any(PurePosixPath(home).is_relative_to(real) for _, real in given)


async def test_a_directory_written_in_the_controllers_home_that_resolves_elsewhere_is_named_as_written(
    linux_links, caplog
) -> None:
    # An operator's link in the home: what the CLI writes in its target is what the controller finds at the link, in
    # its home. The line names the path that lies there, and where it resolves.
    linux_links.dirs.add("/opt/out")
    linux_links.links["/home/controller/out-link"] = "/opt/out"
    settings = {"CLI_SANDBOX_WRITABLE": "/home/controller/out-link"}

    with caplog.at_level(logging.WARNING):
        await _launch_on_linux(_linux_settings(**settings), "/data/cli-home", {"HOME": "/data/cli-home"})
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(
            _linux_settings(CLI_SANDBOX_REQUIRED="true", **settings), "/data/cli-home", {"HOME": "/data/cli-home"}
        )

    (line,) = _written(caplog)
    assert "write '/home/controller/out-link' ('/opt/out' once resolved) under" in line
    assert "which lies in the controller's own home '/home/controller'" in line


@pytest.mark.parametrize("entry", ["/home/controller/.cache/", "/home/controller/.cache/.", "/home/controller//.cache"])
async def test_a_directory_written_in_the_controllers_home_with_a_trailing_slash_is_named_once_as_it_is(
    linux_links, caplog, entry: str
) -> None:
    # B5-fix3 item 1 (O3-1): '/home/controller/.cache/' is '/home/controller/.cache' to Linux; the line named it with
    # "('/home/controller/.cache' once resolved)", as if it were a link.
    linux_links.dirs.add("/home/controller/.cache")

    with caplog.at_level(logging.WARNING):
        await _launch_on_linux(
            _linux_settings(CLI_SANDBOX_WRITABLE=entry), "/data/cli-home", {"HOME": "/data/cli-home"}
        )

    (line,) = _written(caplog)
    assert "provider CLIs may write '/home/controller/.cache' under the Landlock sandbox, which lies" in line, line
    assert "once resolved" not in line


@pytest.mark.parametrize(
    "entry, said",
    [
        ("/mnt/home", "and the controller's own home %s lies there"),
        ("/mnt/home/controller/work", "which lies in the controller's own home %s:"),
    ],
    ids=["a directory given holds the home", "a directory given lies in the home"],
)
async def test_the_controllers_home_that_a_directory_given_meets_once_resolved_is_named_with_where_it_resolves(
    linux_links, caplog, entry: str, said: str
) -> None:
    # HOME and the passwd entry name a link, and the directory given meets the home where the link leads: the line
    # names both, as it names the directory.
    linux_links.home = linux_links.passwd_home = "/home/c-link"
    linux_links.links["/home/c-link"] = "/mnt/home/controller"
    linux_links.dirs.add("/mnt/home/controller/work")
    linux_links.user_base = "/opt/ub"

    with caplog.at_level(logging.WARNING):
        await _launch_on_linux(
            _linux_settings(CLI_SANDBOX_WRITABLE=entry), "/data/cli-home", {"HOME": "/data/cli-home"}
        )

    (line,) = _written(caplog)
    assert said % "'/home/c-link' ('/mnt/home/controller' once resolved)" in line


@pytest.mark.parametrize(
    "layout", ["PYTHONUSERBASE in /tmp", "the passwd home's .local as CLI_HOME", "the image's own layout"]
)
async def test_what_the_controller_reads_at_its_start_is_reported_where_a_cli_may_write_it_whatever_home_it_is_in(
    linux_links, caplog, layout: str
) -> None:
    # Python's user base is where PYTHONUSERBASE puts it, /tmp among the places, and each home has its .local: a
    # usercustomize.py or a .pth a CLI leaves in one runs at the controller's next start, outside the sandbox.
    home = "/data/cli-home"
    if layout == "PYTHONUSERBASE in /tmp":
        linux_links.user_base = "/tmp/controller-python"
        linux_links.dirs.add(linux_links.user_base)
        place = linux_links.user_base
    elif layout == "the passwd home's .local as CLI_HOME":  # HOME, and the user base, are another home's
        linux_links.passwd_home = "/home/botuser"
        home = place = "/home/botuser/.local/lib"
        linux_links.dirs |= {home, "/home/botuser/.ssh"}
    env = {"HOME": home}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(), home, env)

    if layout == "the image's own layout":
        assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]
        assert _written(caplog) == []
        assert await _launch_on_linux(_linux_settings(CLI_SANDBOX_REQUIRED="true"), home, env)
        return
    (line,) = _written(caplog)
    assert place in line and "next start" in line
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(_linux_settings(CLI_SANDBOX_REQUIRED="true"), home, env)


@pytest.mark.parametrize(
    "layout, named",
    [
        ("a checkout in /tmp", "/tmp/auto-browser/controller/app"),
        ("an interpreter that resolves into the CLI home", "/usr/local/bin/python3"),
        ("a venv in /var/tmp", "/var/tmp/venv/bin/python"),
        ("packages in a directory an entry of CLI_SANDBOX_WRITABLE lies in", "/srv/site-packages"),
    ],
)
async def test_where_the_controller_runs_its_code_from_is_reported_where_a_cli_may_write_it(
    linux_links, monkeypatch, caplog, layout: str, named: str
) -> None:
    # A host run from a checkout or a venv a CLI may write: what a CLI leaves in the launcher, the controller's
    # package, its interpreter or its packages, the controller runs outside the sandbox, at the next call or start.
    entry = ""
    if layout == "a checkout in /tmp":
        _controller_runs_from(monkeypatch, launcher="/tmp/auto-browser/controller/app/cli_sandbox_launcher.py")
    elif layout == "an interpreter that resolves into the CLI home":
        linux_links.dirs.add("/data/cli-home/python/bin")
        linux_links.links["/usr/local/bin/python3"] = "/data/cli-home/python/bin/python3.12"
    elif layout == "a venv in /var/tmp":
        linux_links.links["/var/tmp/venv/bin/python"] = "/usr/local/bin/python3.12"
        venv = "/var/tmp/venv"
        _controller_runs_from(
            monkeypatch,
            executable=venv + "/bin/python",
            prefix=venv,
            site_packages=venv + "/lib/python3.12/site-packages",
        )
    else:
        entry = "/srv/site-packages/cache"
        linux_links.dirs.add(entry)
        _controller_runs_from(monkeypatch, site_packages="/srv/site-packages")

    command = await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE=entry)

    assert _given_to_write(command)[:4] == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]


@pytest.mark.parametrize(
    "layout, named",
    [
        ("a PYTHONPATH entry in /tmp", "/tmp/controller-packages"),
        ("started from a directory in /var/tmp, '' on its path", "/var/tmp/run"),
        ("a directory a .pth adds, in the CLI home", "/data/cli-home/lib"),
        ("an entry with a '..' after a link into the CLI home", "/data/cli-home/packages"),
    ],
)
async def test_an_entry_of_the_controllers_import_path_that_a_cli_may_write_is_reported(
    linux_links, monkeypatch, caplog, layout: str, named: str
) -> None:
    # The controller imports from every entry of sys.path at its next start: a json.py a CLI leaves in one is run
    # in the standard library's place, outside the sandbox. Python reads an entry as written (B5-fix1 item 4, C-1):
    # /opt/alias/slot/../packages is the packages beside the slot's target, in the CLI home.
    import_path, clause = list(_IMAGE_IMPORT_PATH), "and the controller reads %r"
    if layout == "a PYTHONPATH entry in /tmp":
        import_path.insert(1, named)
    elif layout.startswith("started from"):
        linux_links.cwd = named
        import_path[1] = ""
    elif layout.startswith("an entry with a '..'"):
        linux_links.links["/opt/alias/slot"] = "/data/cli-home/sub"
        import_path.append("/opt/alias/slot/../packages")
        clause = "and the controller reads '/opt/alias/slot/../packages' (%r once resolved)"
    else:
        import_path.append(named)
    linux_links.dirs.add(named)
    _controller_runs_from(monkeypatch, import_path=tuple(import_path))

    command = await _reads_on_linux(caplog, named, clause=clause)

    assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]


@pytest.mark.parametrize(
    "path, cwd, entry, named",
    [
        (_IMAGE_PATH, "/app", "/usr/local/bin", "/usr/local/bin"),
        ("/tmp/bin:" + _IMAGE_PATH, "/app", None, "/tmp/bin"),
        ("bin:" + _IMAGE_PATH, "/app", None, None),
        (":/usr/bin", "/tmp/run", None, "/tmp/run"),
        (".:/usr/bin", "/tmp/run", None, "/tmp/run"),
        ("bin:/usr/bin", "/tmp/run", None, "/tmp/run/bin"),
        (None, "/app", "/bin", "/bin"),
        (None, "/srv/run", "/srv/run/cache", None),
    ],
    ids=[
        "a distribution's Python, /usr/local/bin given",
        "a PATH directory in /tmp",
        "a relative PATH entry, read from a working directory no CLI may write",
        "an empty PATH entry",
        "a '.' PATH entry",
        "a relative PATH entry",
        "no PATH, where exec searches /bin and /usr/bin",
        "no PATH, and a directory given in the working directory, which exec does not search",
    ],
)
async def test_a_directory_of_the_controllers_path_that_a_cli_may_write_is_reported(
    linux_links, monkeypatch, caplog, path: str | None, cwd: str, entry: str | None, named: str | None
) -> None:
    # B5-fix1 item 2 (OC-10): the controller runs tesseract, autossh, the verifier's command and the CLIs themselves
    # by their name on its PATH, outside the sandbox: a CLI that writes a tesseract there runs as the controller at
    # the next OCR. /usr/local/bin is where npm links the CLIs it updates in /usr/local/lib/node_modules. exec reads
    # a relative entry, the empty one among them, from the working directory (B5-fix1 item 4), and searches
    # os.defpath where PATH is not set, not the working directory (B5-fix2 item 3, C2-3).
    if path is None:
        del linux_links.os.environ["PATH"]
    else:
        linux_links.os.environ["PATH"] = path
    linux_links.cwd = cwd
    linux_links.dirs.update({"/usr/local/bin", "/tmp/bin", "/tmp/run/bin", "/bin", "/srv/run/cache"})
    _controller_runs_from(
        monkeypatch,
        executable="/usr/bin/python3",
        prefix="/usr",
        base_prefix="/usr",
        stdlib="/usr/lib/python3.11",
        site_packages="/usr/lib/python3/dist-packages",
        import_path=("/opt/auto-browser/controller", "/usr/lib/python3.11", "/usr/lib/python3/dist-packages"),
    )

    await _reads_on_linux(caplog, named, **({"CLI_SANDBOX_WRITABLE": entry} if entry else {}))


@pytest.mark.parametrize(
    "links, entry, verifier, browsers, named",
    [
        (
            {"/usr/local/bin/tesseract": "/usr/local/lib/toolchain/tesseract"},
            "/usr/local/lib/toolchain",
            "",
            None,
            "/usr/local/bin/tesseract",
        ),
        ({}, "/opt/verifier/bin", "/opt/verifier/bin/verify --strict", None, "/opt/verifier/bin/verify"),
        ({}, "/opt/verifier/lib", "python3 /opt/verifier/lib/run.py", None, "/opt/verifier/lib/run.py"),
        (
            {"/usr/local/bin/verify": "../lib/node_modules/verifier/bin/verify.js"},
            "/usr/local/lib/node_modules",
            "verify",
            None,
            "/usr/local/bin/verify",
        ),
        ({}, "", "", "/tmp/ms-playwright", "/tmp/ms-playwright"),
        ({}, "", "", "0", None),
        ({}, "", "verify 'unclosed", None, None),
    ],
    ids=[
        "a PATH link to a tesseract in a directory given",
        "an absolute verifier command in a directory given",
        "a script the verifier's command runs, in a directory given",
        "an npm verifier on PATH, with the docs' node_modules given",
        "PLAYWRIGHT_BROWSERS_PATH in /tmp",
        "PLAYWRIGHT_BROWSERS_PATH 0, the package's own browsers",
        "a verifier command that cannot be split, which no verifier runs",
    ],
)
async def test_a_program_the_controller_runs_outside_the_sandbox_that_a_cli_may_replace_is_reported(
    linux_links, caplog, links: dict[str, str], entry: str, verifier: str, browsers: str | None, named: str | None
) -> None:
    # B5-fix2 item 1 (C2-2, H-1, OC2-6): a PATH directory judged whole says nothing of a link in it to a file
    # elsewhere, nor of a verifier named by its path or a script it runs: a CLI given where one of them lies replaces
    # what the controller runs as itself at the next OCR, tunnel or verification, or shadow browse's Chromium.
    linux_links.links.update(links)
    if entry:
        linux_links.dirs.add(entry)
    if browsers is not None:
        linux_links.os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers

    await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE=entry, HARNESS_UV_COMMAND=verifier)


_NODE_VERIFIER = {"HARNESS_UV_COMMAND": "node /opt/uv/verify.js", "HARNESS_UV_ENV_PASSTHROUGH": "NODE_PATH"}
_NODE_OPTIONS_VERIFIER = {"HARNESS_UV_COMMAND": "node /opt/uv/verify.js", "HARNESS_UV_ENV_PASSTHROUGH": "NODE_OPTIONS"}
_ADDRESSES = {
    "HTTP_PROXY": "http://proxy:3128",
    "TZ": "America/Chicago",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
}


@pytest.mark.parametrize(
    "variables, cwd, entry, changed, named",
    [
        ({}, "/app", "", {}, None),
        ({"TESSDATA_PREFIX": "/srv/tools/tessdata"}, "/app", "/srv/tools", {}, "/srv/tools/tessdata"),
        ({"DEBUG_FILE": "/tmp/pw.log"}, "/app", "", {}, "/tmp/pw.log"),
        ({"AUTOSSH_PIDFILE": "/tmp/a.pid"}, "/app", "", {}, "/tmp/a.pid"),
        ({"AUTOSSH_PATH": "/srv/tools/ssh"}, "/app", "/srv/tools", {}, "/srv/tools/ssh"),
        ({"PLAYWRIGHT_NODEJS_PATH": "/srv/tools/node"}, "/app", "/srv/tools", {}, "/srv/tools/node"),
        ({"SSL_CERT_FILE": "/srv/tools/ca.pem"}, "/app", "/srv/tools", {}, "/srv/tools/ca.pem"),
        ({"NODE_PATH": "/srv/a:/srv/tools"}, "/app", "/srv/tools", _NODE_VERIFIER, "/srv/tools"),
        (
            {"NODE_PATH": "/srv/a:/srv/tools"},
            "/app",
            "/srv/tools",
            {**_NODE_VERIFIER, "HARNESS_VERIFIER": "programmatic"},
            None,
        ),
        (_ADDRESSES, "/app", "/srv/tools", {}, None),
        ({"DEBUG_FILE": "logs/pw.log"}, "/srv/run", "/srv/run/logs", {}, "/srv/run/logs/pw.log"),
        ({"DEBUG_FILE": "pw.log"}, "/srv/run", "/srv/run", {}, None),
        (
            {"NODE_OPTIONS": "--require=/srv/tools/hook.js"},
            "/app",
            "/srv/tools",
            _NODE_OPTIONS_VERIFIER,
            "/srv/tools/hook.js",
        ),
        (
            {"NODE_OPTIONS": "--require /srv/tools/hook.js"},
            "/app",
            "/srv/tools",
            _NODE_OPTIONS_VERIFIER,
            "/srv/tools/hook.js",
        ),
        ({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-x"}, "/app", "", {}, "/tmp/dbus-x"),
    ],
    ids=[
        "the image's own environment and layout",
        "TESSDATA_PREFIX, where Tesseract reads its parameters, file writes among them",
        "DEBUG_FILE, which Playwright's driver writes",
        "AUTOSSH_PIDFILE, which autossh writes",
        "AUTOSSH_PATH, the ssh autossh runs",
        "PLAYWRIGHT_NODEJS_PATH, the node Playwright runs as its driver",
        "SSL_CERT_FILE, a base name every one of them gets",
        "a name HARNESS_UV_ENV_PASSTHROUGH hands the verifier, as a ':' list",
        "the same name where HARNESS_VERIFIER runs no command",
        "values that name no place: a proxy, a time zone, a bus address",
        "a relative value with a '/', read from the working directory",
        "a value with no '/', not judged: name the file by an absolute path",
        "NODE_OPTIONS passed through, a path after '='",
        "NODE_OPTIONS passed through, a path as its own word",
        "a D-Bus address naming a socket in /tmp, after 'path='",
    ],
)
async def test_a_place_a_variable_handed_to_a_program_run_outside_the_sandbox_names_is_judged(
    linux_links,
    monkeypatch,
    caplog,
    variables: dict[str, str],
    cwd: str,
    entry: str,
    changed: dict[str, str],
    named: str | None,
) -> None:
    # B5-fix6 item 5 (O6-5, O6-6, O6-7, O6-8): the controller hands Tesseract, Playwright's driver, autossh and the
    # verifier variables of its own environment, and some name a place each reads, writes or runs. With
    # TESSDATA_PREFIX in a directory a CLI may write, a CLI set Tesseract's parameters (tessedit_write_params_to_file
    # and debug_file overwrote files: Opus's tess_probe3, the controller's own OCR call in the image); the driver
    # truncated a link's target at DEBUG_FILE, and autossh followed links at AUTOSSH_PIDFILE and AUTOSSH_LOGFILE.
    # None was judged; PLAYWRIGHT_BROWSERS_PATH, which is, was the one. B5-fix7 item 5 (O7-4, O7-13 = C7-8): judged as
    # one path, a value hid what its words name (NODE_OPTIONS=--require=/srv/tools/hook.js, which node loads) and a
    # D-Bus socket after path=; a value is now judged as a later word of the verifier's command is, by one helper.
    linux_links.cwd = cwd
    linux_links.os.environ.update(variables)
    linux_links.dirs.update({"/srv/tools", "/srv/run/logs", "/opt/uv"})
    if cwd != "/app":
        _controller_runs_from(monkeypatch, import_path=tuple(place for place in _IMAGE_IMPORT_PATH if place != "."))
    given_to_write = {"CLI_SANDBOX_WRITABLE": entry} if entry else {}

    await _reads_on_linux(caplog, named, **given_to_write, **changed)


_RUNS_VERIFY = {"HARNESS_UV_COMMAND": "/opt/uv/verify"}
_PW_SLOT = {"/opt/pw/slot": "/srv/tools/sub"}


@pytest.mark.parametrize(
    "variables, home, links, entry, changed, named",
    [
        ({"TMPDIR": "/tmp"}, "/data/cli-home", {}, "", {**_RUNS_VERIFY, "HARNESS_UV_ENV_PASSTHROUGH": "TMPDIR"}, None),
        (
            {"HOME": "/home/controller"},
            "/home/controller/cli-home",
            {},
            "",
            {**_RUNS_VERIFY, "HARNESS_UV_ENV_PASSTHROUGH": "HOME"},
            None,
        ),
        (
            {"PLAYWRIGHT_BROWSERS_PATH": "/opt/pw/slot/../verify"},
            "/data/cli-home",
            _PW_SLOT,
            "/srv/tools",
            {**_RUNS_VERIFY, "HARNESS_UV_ENV_PASSTHROUGH": "PLAYWRIGHT_BROWSERS_PATH"},
            "/opt/pw/slot/../verify",
        ),
        ({"PLAYWRIGHT_BROWSERS_PATH": "/opt/pw/slot/../verify"}, "/data/cli-home", _PW_SLOT, "/srv/tools", {}, None),
        ({"XAUTHORITY": "/tmp/xvfb-run.abc/Xauthority"}, "/data/cli-home", {}, "", {}, None),
        ({"XDG_RUNTIME_DIR": "/run/user/0"}, "/run/user/0/auto-browser-cli", {}, "", {}, None),
        ({"XDG_RUNTIME_DIR": "/tmp/runtime-root"}, "/data/cli-home", {}, "", {}, "/tmp/runtime-root"),
        (
            {"NODE_PATH": "/srv/tools"},
            "/data/cli-home",
            {},
            "/srv/tools",
            {**_RUNS_VERIFY, "HARNESS_UV_ENV_PASSTHROUGH": "node_path"},
            "/srv/tools",
        ),
        ({"OMP_TOOL_LIBRARIES": "/srv/tools/tool.so"}, "/data/cli-home", {}, "/srv/tools", {}, "/srv/tools/tool.so"),
    ],
    ids=[
        "TMPDIR passed through, which the verifier gets as the controller's own temp directory",
        "HOME passed through, with a CLI home in the controller's home",
        "PLAYWRIGHT_BROWSERS_PATH passed through, as the verifier gets it: a link before '..'",
        "PLAYWRIGHT_BROWSERS_PATH not passed through, as the driver reads it",
        "XAUTHORITY in /tmp, as xvfb-run puts it",
        "XDG_RUNTIME_DIR holding the CLI home, as the controller's own fallback has it",
        "XDG_RUNTIME_DIR in /tmp",
        "a lower-case name passed through, which allowlisted_environment hands upper-cased (mut7 H32)",
        "an OMP_ name Tesseract's OpenMP loads a library by (mut7 H33)",
    ],
)
async def test_each_name_handed_to_a_program_run_outside_the_sandbox_is_judged_as_that_program_gets_it(
    linux_links,
    caplog,
    variables: dict[str, str],
    home: str,
    links: dict[str, str],
    entry: str,
    changed: dict[str, str],
    named: str | None,
) -> None:
    # B5-fix7 item 6 (C7-6 = O7-6, C7-7, O7-5, O7-9): a name passed through to the verifier was judged by the
    # controller's own value where the verifier never gets it (TMPDIR and the other temp names: the controller's own
    # temp directory; HOME, judged as the controller's home), and not where it gets it raw (PLAYWRIGHT_BROWSERS_PATH,
    # whose driver's view stays excluded: _browsers judges it). XAUTHORITY is only read, a cookie a CLI that may write
    # it can read already; XDG_RUNTIME_DIR is judged where it is, or lies in, a directory given, not where it holds one
    # (a CLI home in it). The last two rows guard what mut7 found no row for.
    linux_links.os.environ.update(variables)
    linux_links.links.update(links)
    linux_links.dirs.update({"/srv/tools/sub", "/opt/pw", home})
    given_to_write = {"CLI_SANDBOX_WRITABLE": entry} if entry else {}

    await _reads_on_linux(caplog, named, home, **given_to_write, **changed)


def test_the_base_names_judged_as_places_are_base_names() -> None:
    # B5-fix6 item 5: BASE_PLACE_NAMES reach every program the controller runs outside the sandbox only as base names.
    from app.providers.base import CLI_BASE_ENV_NAMES

    assert set(cli_sandbox.BASE_PLACE_NAMES) <= CLI_BASE_ENV_NAMES


# An extension module for -m mod, named in this Python's own first extension suffix: a Windows host's Python names
# none .so.
_EXTENSION_MODULE = "/srv/run/mod" + importlib.machinery.EXTENSION_SUFFIXES[0]


@pytest.mark.parametrize(
    "cwd, links, entry, verifier, named",
    [
        (
            "/srv/run",
            {"/srv/run/verify.py": "/srv/tools/verify.py"},
            "/srv/tools",
            "python3 verify.py",
            "/srv/run/verify.py",
        ),
        (
            "/srv/run",
            {"/srv/run/verify.js": "/srv/tools/verify.js"},
            "/srv/tools",
            "node verify.js",
            "/srv/run/verify.js",
        ),
        ("/srv/run", {}, "/srv/run/uv_verifier", "python3 -m uv_verifier", "/srv/run/uv_verifier"),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -m mod", "/srv/run/mod.py"),
        (
            "/srv/run",
            {"/srv/run/pkg/sub.py": "/srv/tools/sub.py"},
            "/srv/tools",
            "python3 -m pkg.sub",
            "/srv/run/pkg/sub.py",
        ),
        (
            "/srv/run",
            {"/srv/run/pkg/__init__.py": "/srv/tools/init.py"},
            "/srv/tools",
            "python3 -m pkg.sub",
            "/srv/run/pkg/__init__.py",
        ),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -mmod", "/srv/run/mod.py"),
        (
            "/srv/run",
            {"/srv/run/pkg/__main__.py": "/srv/tools/main.py"},
            "/srv/tools",
            "python3 -m pkg",
            "/srv/run/pkg/__main__.py",
        ),
        ("/srv/run", {"/srv/run/mod.pyc": "/srv/tools/mod.pyc"}, "/srv/tools", "python3 -m mod", "/srv/run/mod.pyc"),
        (
            "/srv/run",
            {_EXTENSION_MODULE: "/srv/tools/mod.so"},
            "/srv/tools",
            "python3 -m mod",
            _EXTENSION_MODULE,
        ),
        ("/srv/run", {"/srv/run/a.py": "/srv/tools/a.py"}, "/srv/tools", "python3 -m a.b", "/srv/run/a.py"),
        (
            "/srv/run",
            {"/srv/run/pkg/__init__.py": "/srv/tools/init.py"},
            "/srv/tools",
            "python3 -m pkg",
            "/srv/run/pkg/__init__.py",
        ),
        (
            "/srv/run",
            {"/srv/run/pkg/__main__.pyc": "/srv/tools/main.pyc"},
            "/srv/tools",
            "python3 -m pkg",
            "/srv/run/pkg/__main__.pyc",
        ),
        (
            "/srv/run",
            {"/srv/run/a/b/__init__.py": "/srv/tools/init.py"},
            "/srv/tools",
            "python3 -m a.b.c",
            "/srv/run/a/b/__init__.py",
        ),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -um mod", "/srv/run/mod.py"),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -Bm mod", "/srv/run/mod.py"),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -uBmmod", "/srv/run/mod.py"),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "uv run --module mod", "/srv/run/mod.py"),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -Rm mod", "/srv/run/mod.py"),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -tm mod", "/srv/run/mod.py"),
        ("/srv/run", {"/srv/run/-mmod.py": "/srv/tools/mod.py"}, "/srv/tools", "python3 -m -mmod", "/srv/run/-mmod.py"),
        (
            "/srv/run",
            {"/srv/run/mod.py": "/srv/tools/mod.py"},
            "/srv/tools",
            "uv run --module --no-project mod",
            "/srv/run/mod.py",
        ),
        (
            "/srv/run",
            {"/srv/run/mod.py": "/srv/tools/mod.py"},
            "/srv/tools",
            "uv run --module -- mod",
            "/srv/run/mod.py",
        ),
        (
            "/srv/run",
            {"/srv/run/mod.py": "/srv/tools/mod.py"},
            "/srv/tools",
            "uv run -m --offline mod",
            "/srv/run/mod.py",
        ),
        ("/srv/run", {"/srv/run/mod.py": "/srv/tools/mod.py"}, "/srv/tools", "uv run -nm mod", "/srv/run/mod.py"),
        (
            "/srv/run",
            {"/srv/run/pkg/__init__.py": "/srv/tools/init.py"},
            "/srv/tools",
            "python3 -m pkg.bad/name",
            "/srv/run/pkg/__init__.py",
        ),
        (
            "/srv/run",
            {"/srv/run/pkg/__init__.py": "/srv/tools/init.py"},
            "/srv/tools",
            "python3 -mpkg.bad/name",
            "/srv/run/pkg/__init__.py",
        ),
        ("/srv/run", {"/srv/run/__init__.py": "/srv/tools/init.py"}, "/srv/tools", "python3 ./tmp/verify.py", None),
        (
            "/srv/run",
            {"/srv/run/mod.py": "/srv/tools/mod.py"},
            "/srv/tools",
            "python3 -m pytest -pmod",
            "/srv/run/mod.py",
        ),
        ("/app", {}, "/srv/tools", "python3 -m pkg.sub", None),
        ("/app", {}, "/srv/tools", "/opt/uv/verify --config=/srv/tools/v.toml", "/srv/tools/v.toml"),
        ("/app", {}, "/srv/tools", "/opt/uv/verify -o/srv/tools/out.json", "/srv/tools/out.json"),
        ("/app", {}, "/srv/tools", "/opt/uv/verify -I/srv/tools/inc", "/srv/tools/inc"),
        ("/app", {}, "/srv/tools", "/opt/uv/verify -I/srv/tools:/a", "/srv/tools"),
        ("/app", {}, "/srv/tools", "python3 -X pycache_prefix=/srv/tools verify2.py", "/srv/tools"),
        ("/app", {}, "/srv/tools", "env PYTHONPATH=/srv/tools python3 -m plain", "/srv/tools"),
        ("/app", {}, "/srv/tools", "env PYTHONPATH=/a:/srv/tools python3 -m plain", "/srv/tools"),
        ("/app", {}, "/srv/tools", "env PATH=/srv/tools:/usr/bin verify", "/srv/tools"),
        ("/app", {}, "/srv/tools", "/opt/uv/verify --path /a:/srv/tools", "/srv/tools"),
        ("/app", {}, "/srv/tools", "java -cp /srv/a:/srv/tools Verify", "/srv/tools"),
        ("/app", {}, "", "/opt/uv/verify --url=http://host:8080/x", None),
        ("/app", {}, "", "/opt/uv/verify --url=http://dev:8080/api", None),
        ("/app", {}, "", "/opt/uv/verify --url=http://dev/x", None),
        ("/app", {}, "", "/opt/uv/verify --url=http://tmp:8080/", None),
        ("/app", {}, "", "/opt/uv/verify --dest=host:/tmp/x", "/tmp/x"),
        ("/app", {}, "", "/opt/uv/verify --db=sqlite:////tmp/verifier.db", "/tmp/verifier.db"),
        ("/app", {}, "/srv/tools", "/opt/uv/verify --config=file:///srv/tools/v.toml", "/srv/tools/v.toml"),
        ("/app", {}, "/srv/tools", "env PYTHONPATH=/opt/uv://srv/tools python3 /opt/uv/v.py", "/srv/tools"),
        ("/app", {}, "/srv/tools", "/opt/uv/verify --path=/srv/tools:http://x/", "/srv/tools"),
        ("/app", {}, "/srv/tools", "env PYTHONPATH=http://example.org:/srv/tools python3 /opt/uv/v.py", "/srv/tools"),
        ("/app", {}, "", "/opt/uv/verify --log /dev/null", "/dev/null"),
        ("/app", {}, "", "/opt/uv/verify --log /tmp/uv.log", "/tmp/uv.log"),
        ("/app", {}, "", "python3 /opt/uv/verify.py --strict", None),
        ("/app", {}, "", "verify -x", None),
        ("/app", {}, "", "python3 ./tmp/verify.py", None),
        ("/app", {}, "", "/opt/uv/verify --output=./tmp/result.json", None),
    ],
    ids=[
        "a script named bare, a link into a directory given",
        "a node script named bare, a link into a directory given",
        "a module run with -m, a directory given",
        "-m mod, its .py a link into a directory given",
        "-m pkg.sub, its .py a link into a directory given",
        "-m pkg.sub, its package's __init__.py a link into a directory given",
        "-mmod, its .py a link into a directory given",
        "-m pkg, its __main__.py a link into a directory given",
        "-m mod, a sourceless mod.pyc a link into a directory given",
        "-m mod, an extension module a link into a directory given",
        "-m a.b, a module file a.py on the way a link into a directory given",
        "-m pkg, its own __init__.py a link into a directory given",
        "-m pkg, a sourceless __main__.pyc a link into a directory given",
        "-m a.b.c, the middle package's __init__.py a link into a directory given",
        "-um mod, flags without an argument then m",
        "-Bm mod",
        "-uBmmod, the name attached to the cluster",
        "uv's --module mod",
        "-Rm mod, R taking no argument",
        "-tm mod, t taking no argument",
        "-m -mmod, whose module Python takes as it is",
        "uv's --module, a flag, then an option before the module",
        "uv's --module, then -- before the module",
        "uv's -m, a flag, then an option before the module",
        "uv's -nm, a cluster of its flags",
        "-m pkg.bad/name, whose package Python imports before it fails",
        "-mpkg.bad/name, attached",
        "./tmp/verify.py, whose first part is empty, names no module: no __init__.py in the working directory judged",
        "pytest's -pmod, the part after the first letter",
        "-m pkg.sub from the image's working directory, nothing there given",
        "an --option=value naming a file in a directory given",
        "a short option with a file in a directory given stuck to it, -o/srv/tools/out.json",
        "a short option with a directory given stuck to it, -I/srv/tools/inc",
        "a ':' list after a short option's letter, -I/srv/tools:/a",
        "-X pycache_prefix= naming a directory given, where the verifier's Python reads .pyc files",
        "env's PYTHONPATH= naming a directory given",
        "a ':' list after '=', PYTHONPATH=/a:/srv/tools",
        "a ':' list after '=', PATH=/srv/tools:/usr/bin",
        "a ':' list as its own word, --path /a:/srv/tools",
        "a ':' list as its own word, -cp /srv/a:/srv/tools",
        "a URL's ':' from the image's working directory, nothing given",
        "a URL whose host and port would split into //dev, /dev",
        "a URL whose path would split into //dev/x",
        "a URL whose host would split into //tmp, /tmp",
        "an scp-style host:/path, read as a ':' list: over-judged",
        "a sqlite: URL's path after four slashes",
        "a file: URL's path after three slashes",
        "a ':' list with a '//' part and no URL",
        "a ':' list with a URL after a path",
        "a ':' list with a path after a URL",
        "a log in /dev, which CLIs may write",
        "a log in /tmp, which CLIs may write",
        "the image's own layout",
        "a bare word and an option, from the image's working directory",
        "a script by a relative path through a directory, from the image's working directory",
        "an --option=value by a relative path through a directory, from the image's working directory",
    ],
)
async def test_each_later_word_of_the_verifiers_command_is_judged_as_a_path(
    linux_links, monkeypatch, caplog, cwd: str, links: dict[str, str], entry: str, verifier: str, named: str | None
) -> None:
    # B5-fix3 item 3 (C3-1 = O3-3, C3-3 = O3-6): the verifier runs as the controller's user outside the sandbox, and
    # each later word may name what it runs (python3 verify.py, -m uv_verifier) or a file it reads or writes, read
    # from its working directory where it is relative. Judged only where it held a '/', a bare script name went
    # unjudged. A file it writes in /dev or /tmp stays reported: a link a CLI leaves at that name takes what it
    # writes where the CLI chose (a CLI that runs as root can replace /dev/null). B5-fix4 item 1 (C4-1 = O4-1): a
    # module run with -m is judged by the files Python runs for it from the working directory, not by its name alone.
    # B5-fix5 item 1 (C5-1 = O5-3): in every form this Python imports (a sourceless .pyc, an extension module), for
    # each name on the way (a.py runs before Python says a is no package) and the module itself. B5-fix5 item 2 (R5-2,
    # O5-2): -m at the end of a cluster of Python's flags without an argument (-um mod, -uBmmod), and uv's --module.
    # B5-fix6 item 1 (S6-1; R6-1, C6-1, C6-2, O6-1): reading the command as Python or uv parses it missed -Rm and -tm
    # (R and t take no argument either), -m -mmod (Python takes the word after a bare -m as it is), and uv's module,
    # its first word after its options or '--' (its -m and --module are flags). No word is read as an option now:
    # each later word without a '/' is judged as the module it may name, and so is the rest after the first m of a
    # word led by one '-'. uv rejects --module=mod (exit 2), so that row is gone. B5-fix7 item 3 (C7-2, O7-11): a
    # name holding a '/' is judged too (Python imports pkg before it fails on pkg.bad/name), and a word led by one '-'
    # names its part after its first letter as well (pytest's -pmod names mod). B5-fix6 item 6 (C6-4 = O6-9): a word
    # or value holding '://' is a URL, not a ':' list: split, http://dev:8080/api made //dev, /dev, which CLIs may
    # write, and refused every call under CLI_SANDBOX_REQUIRED=true. Any other ':' still splits (host:/tmp/x).
    # B5-fix7 item 4 (O7-3, C7-5, O7-10): not split at all, a URL hid the path of a file: or sqlite: URL, and a list
    # holding a URL or a '//' part hid its paths. So a list is always split, and only a URL's host is dropped: a part
    # that starts with '//' but not '///' after one that ends in a URL's scheme.
    # A name that holds a '/' is judged by the files of its dotted parents before the first part that holds one, which
    # Python imports before it fails on that part (pkg for pkg.bad/name, none for ./tmp/verify.py): judged whole, the
    # parts of ./tmp/verify.py joined into /tmp/verify/__init__.py, in /tmp, and a script named by a relative path
    # was refused. Of a word led by one '-', the rest after its first letter is judged as paths too where it holds a
    # '/', as a word is: read only as a module name, -o/srv/tools/out.json and -I/srv/tools/inc named no place in
    # /srv/tools. A word whose first part is empty names no module, so no file is judged for it: a link at __init__.py
    # in the working directory, into a directory given, is not reported for ./tmp/verify.py.
    # The rest after a short option's letter is read as a ':' list too, as a word is: -I/srv/tools:/a names /srv/tools.
    linux_links.cwd = cwd
    linux_links.links.update(links)
    linux_links.dirs.update({"/srv/run/uv_verifier", "/srv/tools", "/opt/uv", "/srv/run/pkg"})
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))

    await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE=entry, HARNESS_UV_COMMAND=verifier)


@pytest.mark.parametrize(
    "path, parts",
    [
        ("http://dev:8080/api", ["http", "8080/api"]),
        ("sqlite:////tmp/v.db", ["sqlite", "////tmp/v.db"]),
        ("file:///srv/x", ["file", "///srv/x"]),
        ("http://x/:/srv/x", ["http", "/srv/x"]),
        ("/opt/lib://srv/x", ["/opt/lib", "//srv/x"]),
        ("host:/path", ["host", "/path"]),
        ("x=http\n://tmp", ["x=http\n", "//tmp"]),
        ("/srv/x", []),
    ],
    ids=[
        "a URL's host",
        "a sqlite: URL's path after four slashes",
        "a file: URL's path after three slashes",
        "a path after a URL",
        "a '//' part after a path",
        "an scp-style host:/path, over-judged",
        "a part that ends in a scheme and a newline, which ends no scheme",
        "no ':'",
    ],
)
def test_a_colon_list_is_split_into_its_parts_but_a_urls_host(path: str, parts: list[str]) -> None:
    assert cli_sandbox._list_parts(path) == parts


_VERIFY_ON_PATH = {"/usr/local/bin/verify": "/srv/tools/verify"}


@pytest.mark.parametrize(
    "links, entry, verifier, named",
    [
        ({}, "", "sh -c 'python3 /tmp/v.py'", "/tmp/v.py"),
        ({}, "/srv/tools", "bash -lc 'exec python3 /srv/tools/v.py'", "/srv/tools/v.py"),
        ({}, "/srv/tools", "env -S 'python3 /srv/tools/v.py'", "/srv/tools/v.py"),
        ({}, "", "sh -c '/opt/uv/verify --log /tmp/uv.log'", "/tmp/uv.log"),
        (_VERIFY_ON_PATH, "/srv/tools", "sh -c 'verify --strict'", "/usr/local/bin/verify"),
        ({}, "", "sh -c 'python3 /opt/uv/verify.py'", None),
        ({}, "/srv/tools", "sh -c '\"/srv/tools/verify\"'", "/srv/tools/verify"),
        (_VERIFY_ON_PATH, "/srv/tools", "sh -c verify", "/usr/local/bin/verify"),
        ({}, "/srv/tools", "env -S 'python3\x0b/srv/tools/v.py'", "/srv/tools/v.py"),
        ({}, "/srv/tools", "env -S 'python3\x0c/srv/tools/v.py'", "/srv/tools/v.py"),
        ({}, "/srv/tools", "env -S 'python3 /srv/tools/v.py # \"'", "/srv/tools/v.py"),
        ({}, "", "sh -c 'exec /opt/uv/verify 2>/tmp/uv.log'", "/tmp/uv.log"),
        ({}, "", "sh -c 'exec /opt/uv/verify >>/tmp/uv.log'", "/tmp/uv.log"),
        ({}, "/srv/tools", "sh -c 'exec /opt/uv/verify </srv/tools/in.json'", "/srv/tools/in.json"),
        ({}, "/srv/tools", "sh -c 'true;/srv/tools/v'", "/srv/tools/v"),
        ({}, "", "sh -c \"sh -c 'python3 /tmp/v.py'\"", "/tmp/v.py"),
        ({}, "", "sh -c 'python3\t/tmp/v.py'", "/tmp/v.py"),
        (_VERIFY_ON_PATH, "/srv/tools", "timeout 30 verify", "/usr/local/bin/verify"),
        (_VERIFY_ON_PATH, "/srv/tools", "env verify --strict", "/usr/local/bin/verify"),
        (_VERIFY_ON_PATH, "/srv/tools", "nice -n 5 verify", "/usr/local/bin/verify"),
        ({}, "", "sh -c 'cd / && exec /opt/uv/verify'", None),
        ({}, "", "python3 -c 'print(1 / 2)'", None),
        ({}, "", "/opt/uv/verify --root /", None),
        ({}, "", "/opt/uv/verify --root=//. --base /..", None),
        (
            {"/opt/uv/v;safe.py": "/srv/tools/v.py"},
            "/srv/tools",
            "env -S 'python3 /opt/uv/v;safe.py'",
            "/opt/uv/v;safe.py",
        ),
        (
            {"/opt/uv/v\x0bfile.py": "/srv/tools/v.py"},
            "/srv/tools",
            "sh -c 'python3 /opt/uv/v\x0bfile.py'",
            "/opt/uv/v\x0bfile.py",
        ),
        (
            {"/opt/uv/v\x0cfile.py": "/srv/tools/v.py"},
            "/srv/tools",
            "sh -c 'python3 /opt/uv/v\x0cfile.py'",
            "/opt/uv/v\x0cfile.py",
        ),
        ({"/opt/uv/a#b.py": "/srv/tools/a.py"}, "/srv/tools", "sh -c 'python3 /opt/uv/a#b.py'", "/opt/uv/a#b.py"),
    ],
    ids=[
        "sh -c, a script in /tmp",
        "bash -lc, exec and a script in a directory given",
        "env -S, a script in a directory given",
        "sh -c, a log in /tmp",
        "sh -c, a program exec finds on PATH, a link into a directory given",
        "sh -c, the image's own layout",
        "sh -c, a quoted program in a directory given, a word with no whitespace",
        "sh -c, a bare program on PATH, a link into a directory given",
        "env -S, a vertical tab between its words",
        "env -S, a form feed between its words",
        "env -S, an unmatched quote after a comment, split plain",
        "sh -c, 2> stuck to a log in /tmp",
        "sh -c, >> stuck to a log in /tmp",
        "sh -c, < stuck to a file in a directory given",
        "sh -c, ; stuck to a program in a directory given",
        "sh -c in sh -c, a string nested twice",
        "sh -c, a tab between its words (mut7 H22)",
        "timeout, a program on PATH by a later word",
        "env, a program on PATH by a later word",
        "nice -n, a program on PATH by a later word",
        "sh -c, cd to the root itself, which holds every directory given",
        "-c, a division's '/'",
        "the root itself as an argument",
        "the root itself spelt //. and /..",
        "env -S, a ';' inside a file's name, which GNU env keeps, a link into a directory given",
        "sh -c, a vertical tab inside a file's name, which sh keeps, a link into a directory given",
        "sh -c, a form feed inside a file's name, which sh keeps, a link into a directory given",
        "sh -c, a '#' inside a word, which starts no comment there, a link into a directory given",
    ],
)
async def test_each_later_word_of_the_verifiers_command_is_split_again_as_a_shell_reads_it(
    linux_links, caplog, links: dict[str, str], entry: str, verifier: str, named: str | None
) -> None:
    # B5-fix6 item 4 (O6-10): a shell-wrapped command hid every inner word (Opus's policy_probe4: each allowed, and
    # reported unwrapped). A later word that holds whitespace is split again as the command is, once, and its words
    # are judged as later words are, and each without a '/' also as a program exec finds.
    # B5-fix7 item 1 (C7-1, C7-3, C7-4, O7-1, O7-2, O7-7): that missed a string with no whitespace (sh -c verify, a
    # quoted path), env -S's vertical tab and form feed, a string shlex cannot split (an unmatched quote after a
    # comment), an operator stuck to a path (2>/tmp/uv.log, true;/srv/tools/v), a string nested twice, and a program
    # a wrapper runs by a later word (timeout 30 verify). So every later word is split again as a shell reads it
    # (punctuation its own word, every ASCII whitespace a separator, a plain split where shlex fails), each word that
    # gives again until none is new, and each is judged as a later word and, without a '/', as a program on PATH.
    # B5-fix7 item 2 (O7-8): a word, or a word read in one, that names the root itself (/, //, /., /..) is no place:
    # every directory given lies in it, so judged it refused every call (cd /, a division's '/').
    # A word's shell words are those of both readings: shlex.split's (its own whitespace, no punctuation) keeps a
    # file's name that holds an operator, as GNU env -S does, or a vertical tab or form feed, as sh does (it splits at
    # space and tab); the punctuation reading alone split them and judged none of the whole name. Neither reading
    # starts a comment inside a word.
    linux_links.links.update(links)
    linux_links.dirs.update({"/srv/tools", "/opt/uv"})
    given_to_write = {"CLI_SANDBOX_WRITABLE": entry} if entry else {}

    await _reads_on_linux(caplog, named, HARNESS_UV_COMMAND=verifier, **given_to_write)


@pytest.mark.parametrize(
    "cwd, image_layout, given, verifier, mode, named",
    [
        ("/srv/run", False, "/srv/run/collections", "python3 -m mod", "uv", "/srv/run"),
        ("/srv/run", False, "/srv/run/tools", "python3 -m mod", "uv", "/srv/run"),
        ("/srv/run", False, "/srv/run/collections", "python3 verify.py", "uv", "/srv/run"),
        ("/srv/run", False, "/srv/run/collections", "python3 /opt/uv/verify.py", "uv", "/srv/run"),
        ("/srv/run", False, "/srv/run/collections", "python3 -c 'import json'", "uv", "/srv/run"),
        ("/srv/run", False, "/srv/run/collections", "python3 -Rc 'import json'", "uv", "/srv/run"),
        (
            "/srv/run",
            False,
            "/srv/run/collections",
            "env PYTHONPATH=:/opt/uv python3 /opt/uv/verify.py",
            "uv",
            "/srv/run",
        ),
        ("/srv/run", False, "/srv/run/work", "pytest -q", "uv", "/srv/run"),
        ("/srv/run", False, "/srv/run/collections", "python3 -c 'import json'", "programmatic", None),
        ("/app", True, "", "python3 -m mod", "uv", None),
        ("/app", True, "", "python3 /opt/uv/verify.py", "ensemble", None),
    ],
    ids=[
        "-m, a directory given named as a standard-library module runpy imports",
        "-m, a directory given named as no standard-library module",
        "a script run from there, which Python puts its directory first for",
        "a script named by its path elsewhere, which puts its own directory first: over-judged",
        "-c, which puts '' first",
        "-Rc",
        "an empty part of a PYTHONPATH list",
        "pytest, which collects every test_*.py under it",
        "the same directory given where HARNESS_VERIFIER runs no command",
        "the image's own layout, whose working directory is the controller's own '.', under -m",
        "the image's own layout, a script",
    ],
)
async def test_wherever_the_verifier_runs_the_working_directory_is_an_entry_of_its_import_path(
    linux_links,
    monkeypatch,
    caplog,
    cwd: str,
    image_layout: bool,
    given: str,
    verifier: str,
    mode: str,
    named: str | None,
) -> None:
    # B5-fix5 item 3 (O5-1): under -m, Python puts its working directory first on its import path, and runpy imports
    # standard-library modules from there before the module runs (collections, functools and nine more), as the
    # module then imports what it imports. With the controller's own working directory off its import path (a host
    # run of installed code), a directory given in it ran a CLI's code in the verifier (Opus, end to end in the image
    # with real Landlock). It is judged as the controller's own '.' is.
    # B5-fix6 item 2 (R6-2, R6-3, C6-3 = O6-2, O6-3): not only under -m. Python puts it ('' or its path) first, or a
    # directory in it, under -c, from stdin, for a script run from there and for an empty part of a PYTHONPATH list,
    # and pytest collects every test_*.py under it (the image: "ran cwd/json", "ran cwd/collections"). So it is
    # judged wherever the verifier runs, whatever its command. That over-judges a script named by its path elsewhere
    # (python3 /opt/uv/verify.py puts /opt/uv first, not the working directory), and a native verifier: no narrower
    # rule is safe without trusting a program by its name. Where HARNESS_VERIFIER runs no command, it is not judged.
    linux_links.cwd = cwd
    linux_links.dirs.update({"/srv/run", "/opt/uv", *([given] if given else [])})
    import_path = _IMAGE_IMPORT_PATH if image_layout else tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != ".")
    _controller_runs_from(monkeypatch, import_path=tuple(import_path))
    given_to_write = {"CLI_SANDBOX_WRITABLE": given} if given else {}

    await _reads_on_linux(caplog, named, HARNESS_UV_COMMAND=verifier, HARNESS_VERIFIER=mode, **given_to_write)


_DEFAULT_BROWSERS = "/home/controller/.cache/ms-playwright"


@pytest.mark.parametrize(
    "browsers, links, entry, home, named",
    [
        (None, {_DEFAULT_BROWSERS: "/srv/tools/pw"}, "/srv/tools", "/data/cli-home", _DEFAULT_BROWSERS),
        ("", {_DEFAULT_BROWSERS: "/srv/tools/pw"}, "/srv/tools", "/data/cli-home", _DEFAULT_BROWSERS),
        (None, {}, "", "/home/controller/.cache", _DEFAULT_BROWSERS),
        ("/opt/pw/slot/..", {"/opt/pw/slot": "/srv/tools/x"}, "/srv/tools", "/data/cli-home", None),
        ("pw", {}, "/srv/run", "/data/cli-home", "/srv/run/pw"),
    ],
    ids=[
        "unset: the default place, a link into a directory given",
        "empty, which Playwright takes for unset",
        "unset: CLI_HOME the controller's ~/.cache, which holds the default place",
        "a '..' after a link, which Playwright joins away",
        "a relative one, read from the working directory",
    ],
)
async def test_playwrights_browsers_are_judged_where_playwright_finds_them(
    linux_links,
    monkeypatch,
    caplog,
    browsers: str | None,
    links: dict[str, str],
    entry: str,
    home: str,
    named: str | None,
) -> None:
    # B5-fix3 item 4 (C3-2 + O3-4): Playwright's driver finds the browsers shadow browse runs, outside the sandbox, at
    # PLAYWRIGHT_BROWSERS_PATH joined as path.join and path.resolve join it (normalised, a relative one from its
    # working directory), or where it is unset or empty at $HOME/.cache/ms-playwright: XDG_CACHE_HOME does not reach the
    # driver (process_hardening.confine_playwright_driver_environment). Judged only where set, and as Linux reads it.
    linux_links.cwd = "/srv/run"
    linux_links.links.update(links)
    linux_links.dirs.update({"/srv/tools/x", "/srv/run", "/opt/pw", home})
    if browsers is not None:
        linux_links.os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))

    await _reads_on_linux(caplog, named, home, **({"CLI_SANDBOX_WRITABLE": entry} if entry else {}))


async def test_playwrights_default_place_where_home_is_empty_is_read_from_the_working_directory(
    linux_links, monkeypatch, caplog
) -> None:
    # B5-fix4 item 3 (O4-4): where HOME is set and empty, node's os.homedir() gives "" (Python's expanduser('~') gives
    # '/'), so Playwright looks for the browsers in .cache/ms-playwright from its working directory. On Linux the
    # controller's own home is then '/', which holds every directory a CLI may write, and the home check reports every
    # call (measured in the image); the in-memory Linux keeps /home/controller as the home, so that this row holds the
    # browsers' place alone.
    linux_links.cwd = "/srv/run"
    linux_links.links["/srv/run/.cache/ms-playwright"] = "/srv/tools/pw"
    linux_links.dirs.update({"/srv/tools", "/srv/run/.cache"})
    linux_links.os.environ["HOME"] = ""
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))

    await _reads_on_linux(caplog, "/srv/run/.cache/ms-playwright", CLI_SANDBOX_WRITABLE="/srv/tools")


def test_the_driver_does_not_get_xdg_cache_home_so_playwright_looks_in_home() -> None:
    # B5-fix3 item 4: the default place above holds only while the driver's environment drops XDG_CACHE_HOME.
    from app.process_hardening import PLAYWRIGHT_DRIVER_ENV_NAMES, PLAYWRIGHT_DRIVER_ENV_PREFIXES
    from app.providers.base import CLI_BASE_ENV_NAMES, CLI_BASE_ENV_PREFIXES

    assert "XDG_CACHE_HOME" not in CLI_BASE_ENV_NAMES | PLAYWRIGHT_DRIVER_ENV_NAMES
    assert not "XDG_CACHE_HOME".startswith((*CLI_BASE_ENV_PREFIXES, *PLAYWRIGHT_DRIVER_ENV_PREFIXES))
    assert "HOME" in CLI_BASE_ENV_NAMES


def test_the_programs_run_outside_the_sandbox_by_name_are_those_the_controller_runs() -> None:
    # B5-fix2 item 1: PROGRAMS_RUN_OUTSIDE is the one list of them. pytesseract runs its tesseract_cmd, which nothing
    # in app/ sets (ocr.py only reads it), and the one program app/ starts by a name written in its code is autossh
    # (session_tunnel.py); the rest it starts are its own interpreter, the CLI under the launcher, the verifier's
    # command and the entrypoint's.
    import pytesseract

    spawns = {"create_subprocess_exec", "Popen", "run", "call", "check_call", "check_output", "execvp", "execvpe"}
    named = {pytesseract.pytesseract.tesseract_cmd}
    for path in Path(cli_sandbox.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            called = isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", ""))
            if called not in spawns or not node.args:
                continue
            first = node.args[0].elts[:1] if isinstance(node.args[0], ast.List | ast.Tuple) else node.args[:1]
            named |= {word.value for word in first if isinstance(word, ast.Constant)}

    assert named == set(cli_sandbox.PROGRAMS_RUN_OUTSIDE)


@pytest.mark.parametrize("command", ["verify", "/opt/uv/bin/verify --strict", "python3 '/opt/uv/my run.py' -x"])
def test_the_verifiers_command_is_split_as_the_harness_splits_it(command: str) -> None:
    from app.startup.extensions import _build_harness_verifier

    settings = Settings(_env_file=None, HARNESS_VERIFIER="uv", HARNESS_UV_COMMAND=command)

    assert cli_sandbox._verifier_words(command) == _build_harness_verifier(settings).command


@pytest.mark.parametrize(
    "mode, named",
    [
        (None, None),
        ("", None),
        ("programmatic", None),
        ("uv", "/tmp/uv.log"),
        ("ensemble", "/tmp/uv.log"),
        ("UV", "/tmp/uv.log"),
    ],
    ids=["HARNESS_VERIFIER unset", "empty", "programmatic", "uv", "ensemble", "UV, read lower-cased"],
)
async def test_the_verifiers_command_is_judged_only_where_harness_verifier_runs_it(
    linux_links, caplog, mode: str | None, named: str | None
) -> None:
    # B5-fix6 item 3 (O6-11): startup/extensions.py runs HARNESS_UV_COMMAND only where HARNESS_VERIFIER is uv or
    # ensemble (lower-cased; unset or empty reads as programmatic). Judged wherever it was set, an argument naming a
    # file in /tmp refused every call under CLI_SANDBOX_REQUIRED=true where no verifier runs it.
    await _reads_on_linux(caplog, named, HARNESS_UV_COMMAND="/opt/uv/verify --log /tmp/uv.log", HARNESS_VERIFIER=mode)


@pytest.mark.parametrize("mode", [None, "", "programmatic", "uv", "UV", "ensemble", "Ensemble", "other"])
def test_the_sandbox_reads_harness_verifier_as_the_harness_builds_its_verifier(mode: str | None) -> None:
    # B5-fix6 item 3 (O6-11): the modes that run the command have one source (startup.extensions.runs_uv_command),
    # which _build_harness_verifier reads too, so that the sandbox and the harness cannot drift apart.
    from app.harness.verifier import UniversalVerifierAdapter
    from app.startup.extensions import _build_harness_verifier, runs_uv_command

    settings = _linux_settings(HARNESS_UV_COMMAND="verify", HARNESS_VERIFIER=mode)
    verifier = _build_harness_verifier(settings)

    members = getattr(verifier, "verifiers", [verifier])
    assert any(isinstance(member, UniversalVerifierAdapter) for member in members) == runs_uv_command(settings)
    assert runs_uv_command(settings) == (mode in ("uv", "UV", "ensemble", "Ensemble"))


# A link a CLI can swap, in /tmp, on the way to a '..': /opt/alias/slot leads through /tmp/hop to /srv/safe/sub, so
# /opt/alias/slot/../x is /srv/safe/x now, and wherever the CLI points /tmp/hop next. Normalised it is /opt/alias/x,
# whose way passes no link.
@pytest.mark.parametrize(
    "entry, links, named",
    [
        ("/srv/tools/../controller", {}, None),
        ("/srv/tools/mod", {}, "/srv/tools/mod"),
        ("/srv/glink/x", {"/srv/glink": "/srv/tools"}, "/srv/glink/x"),
        ("/srv/tools/l/../x", {"/srv/tools/l": "/opt/y"}, "/srv/tools/l/../x"),
    ],
    ids=[
        "a '..' through the directory given, back out of it",
        "a place in the directory given",
        "a place past a link that leads to the directory given",
        "a '..' after a link in the directory given",
    ],
)
async def test_an_import_entry_that_passes_through_a_directory_given_is_judged_by_where_linux_reads_it(
    linux_links, monkeypatch, caplog, entry: str, links: dict[str, str], named: str | None
) -> None:
    # B5-fix3 item 2 (O3-2): Linux reads /srv/tools/../controller as /srv/controller: a CLI that writes /srv/tools
    # can make nothing in it that changes where the path leads, as it can with a link it makes in it (the last row).
    # Judged by the spelling's own parents, the entry lay in /srv/tools, and an honest run-time import path was
    # refused under CLI_SANDBOX_REQUIRED=true, which 8e2cc59b allowed.
    linux_links.dirs.update({"/srv/tools", "/srv/controller", "/opt/y", "/opt/x"})
    linux_links.links.update(links)
    _controller_runs_from(monkeypatch, import_path=(*_IMAGE_IMPORT_PATH, entry))

    await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE="/srv/tools")


_HOP = {"/opt/alias/slot": "/tmp/hop", "/tmp/hop": "/srv/safe/sub"}
# What a line says of a directory given that holds the controller's own home.
_OWN_HOME = "and the controller's own home %r"
_PAST_THE_HOP = "/opt/alias/slot/.."


@pytest.mark.parametrize(
    "changes, named",
    [
        ({"import_path": (_PAST_THE_HOP + "/packages",)}, _PAST_THE_HOP + "/packages"),
        ({"PATH": _PAST_THE_HOP + "/packages:/usr/bin"}, _PAST_THE_HOP + "/packages"),
        ({"pycache_prefix": _PAST_THE_HOP + "/pyc"}, _PAST_THE_HOP + "/pyc"),
        ({"passwd_home": _PAST_THE_HOP + "/home", "clause": _OWN_HOME}, _PAST_THE_HOP + "/home"),
        ({"home": _PAST_THE_HOP + "/home", "clause": _OWN_HOME}, _PAST_THE_HOP + "/home"),
        (
            {"launcher": _PAST_THE_HOP + "/src/app/cli_sandbox_launcher.py", "import_path": (_PAST_THE_HOP + "/src",)},
            _PAST_THE_HOP + "/src/app",
        ),
        ({"env_file": _PAST_THE_HOP + "/settings.env"}, _PAST_THE_HOP + "/settings.env"),
        ({"PATH": "/usr/local/bin/../sbin:/usr/bin"}, None),
        ({"cwd": "/srv/run", "import_path": ("../controller",)}, None),
    ],
    ids=[
        "an import entry",
        "a PATH entry",
        "the bytecode prefix",
        "the passwd home",
        "HOME",
        "the launcher, imported through a '..' entry",
        "an env_file",
        "an honest PATH entry with a '..' and no link before it",
        "a relative import entry with a '..' (uvicorn --app-dir ../controller)",
    ],
)
async def test_a_place_read_with_a_dotdot_after_a_link_a_cli_can_swap_is_judged_as_written(
    linux_links, monkeypatch, caplog, changes: dict, named: str | None
) -> None:
    # B5-fix2 item 2 (C2-1, C2-5's variant B): Linux reads a '..' after the link before it. Judged by the normalised
    # spelling, /tmp/hop was no step of any of these places, and a CLI that points it at /data/cli-home/sub chooses
    # the module, the program, the .pyc, the ssh config, the launcher or the settings the controller takes next.
    linux_links.links.update(_HOP)
    linux_links.dirs.update({"/opt/alias", "/srv/safe/sub", "/srv/run", "/srv/controller", "/usr/local/sbin"})
    changes = dict(changes)
    clause = changes.pop("clause", "and the controller reads %r")
    for name in ("cwd", "home", "passwd_home"):
        setattr(linux_links, name, changes.pop(name, getattr(linux_links, name)))
    if "PATH" in changes:
        linux_links.os.environ["PATH"] = changes.pop("PATH")
    if "env_file" in changes:
        monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": changes.pop("env_file")})
    import_path = (*(entry for entry in _IMAGE_IMPORT_PATH if entry != "."), *changes.pop("import_path", ()))
    _controller_runs_from(monkeypatch, import_path=import_path, **changes)

    await _reads_on_linux(caplog, named, clause=clause)


async def test_a_bytecode_prefix_a_cli_may_write_is_reported(linux_links, monkeypatch, caplog) -> None:
    # B5-fix1 item 5 (OC-4): with PYTHONPYCACHEPREFIX=/tmp/pyc, Python imports the .pyc it finds there for json at
    # the next start (OC-4's probe planted one, and it ran). Without one, the image's own layout says nothing
    # (test_what_the_controller_reads_at_its_start_..., "the image's own layout").
    _controller_runs_from(monkeypatch, pycache_prefix="/tmp/pyc")

    await _reads_on_linux(caplog, "/tmp/pyc")


@pytest.mark.parametrize("cwd", ["/tmp/run", "/data/cli-home/run"])
async def test_the_env_file_settings_reads_from_the_working_directory_is_reported_where_a_cli_may_write_it(
    linux_links, monkeypatch, caplog, cwd: str
) -> None:
    # B5-fix1 item 1 (C-2, OC-3): Settings reads .env from the working directory at the start (its env_file), off
    # the import path where the controller runs installed code: a CLI that writes it there sets the next start's
    # settings (CLI_SANDBOX_ENABLED=false, a token of its own). Installed code, started with --app-dir: no '.'.
    linux_links.cwd = cwd
    linux_links.dirs.add(cwd)
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))
    # As config.py ships it: the suite reads none (tests/__init__.py).
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": ".env"})

    command = await _reads_on_linux(caplog, cwd + "/.env")

    # B5-fix4 item 8 (S4-1): normalised, as _ways reads them: an empty entry arrives from _read_from as cwd + '/'.
    # B5-fix5 item 8 (C5-5): as Linux reads them, both sides: a Windows host's os.path.normpath writes backslashes.
    assert posixpath.normpath(cwd) not in {posixpath.normpath(path) for path in cli_sandbox._code("", cwd)}
    assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]


class _EnvFileHandle(os.PathLike):
    """A PathLike env_file whose text says another name than the path it is (C3-4)."""

    def __fspath__(self) -> str:
        return "/data/cli-home/controller.env"

    def __str__(self) -> str:
        return "config-handle"


@pytest.mark.parametrize(
    "env_file, named",
    [
        (_EnvFileHandle(), "/data/cli-home/controller.env"),
        (("",), None),
        (Path(""), None),
        ((".", "cache/settings.env"), "/srv/run/cache/settings.env"),
        ("./", None),
        (["./"], None),
        (["./", "cache/settings.env"], "/srv/run/cache/settings.env"),
        ("././", None),
        (".//", None),
    ],
    ids=[
        "a PathLike, by its path and not its text",
        "an empty name",
        "Path('')",
        "'.' beside a file",
        "'./'",
        "'./' in a list",
        "'./' in a list beside a file",
        "'././'",
        "'.//'",
    ],
)
async def test_an_env_file_name_is_judged_as_settings_reads_it(
    linux_links, monkeypatch, caplog, env_file: object, named: str | None
) -> None:
    # B5-fix3 item 5 (C3-4, C3-6): pydantic-settings reads a PathLike by os.fspath, not str(), and reads no file at
    # "" or "." (a directory, not a file): judged as the working directory, either made /srv/run/cache, which a CLI
    # may write, a place the controller reads. B5-fix4 item 5 (C4-2 = O4-5): nor at any other spelling of "." ("./",
    # "././", ".//"), which Path(name) reads as ".".
    linux_links.cwd = "/srv/run"
    linux_links.dirs.update({"/srv/run/cache", "/data/cli-home"})
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": env_file})

    await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE="/srv/run/cache")


async def test_an_env_file_name_that_is_the_working_directory_once_expanded_is_passed_over(
    linux_links, monkeypatch, caplog
) -> None:
    # B5-fix5 item 5 (C5-4): pydantic-settings expands a leading '~' before it looks for a file, so with HOME "." the
    # name "~/" is "./", the working directory, which is no file: judged as it, it made /srv/run/cache, which a CLI
    # may write, a place the controller reads.
    linux_links.cwd = "/srv/run"
    linux_links.home = "."
    linux_links.dirs.add("/srv/run/cache")
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": "~/"})

    await _reads_on_linux(caplog, None, CLI_SANDBOX_WRITABLE="/srv/run/cache")


# What SettingsConfigDict can set that makes Settings read a file at its start beside env_file (pydantic-settings
# 2.16.2: main.py's SettingsConfigDict, sources/utils.py's _resolve_config_file, BaseSettings' default sources).
_SETTINGS_FILE_OPTIONS = (
    "env_file_depth",
    "secrets_dir",
    "json_file",
    "yaml_file",
    "toml_file",
    "pyproject_toml_depth",
)


def test_settings_finds_no_file_at_its_start_but_by_env_file() -> None:
    # B5-fix3 item 6 (C3-5): _env_files models env_file, each name read from the working directory. With
    # env_file_depth set, Settings would also search the working directory's parents for that name; with secrets_dir,
    # read a file per field there; with a JSON, YAML or TOML source in settings_customise_sources, read that file.
    # None of those is modelled, so none may be on.
    from pydantic_settings import BaseSettings

    defaults = BaseSettings.model_config
    for name in _SETTINGS_FILE_OPTIONS:
        assert Settings.model_config.get(name, defaults.get(name)) == defaults.get(name), name
    assert Settings.settings_customise_sources.__func__ is BaseSettings.settings_customise_sources.__func__
    for path in Path(cli_sandbox.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "Settings":
                passed = {keyword.arg for keyword in node.keywords}
                assert not passed & {"_env_file_depth", "_secrets_dir"}, f"{path.name}:{node.lineno}"


@pytest.mark.parametrize(
    "browsers, entry, reasons",
    [("pw", None, {"browsers"}), (None, "../x", {"dotdot"}), ("pw", "../x", {"dotdot", "browsers"})],
    ids=["a relative PLAYWRIGHT_BROWSERS_PATH alone", "an import entry with a '..' alone", "both"],
)
async def test_the_removed_working_directorys_line_gives_the_reasons_that_apply(
    linux_links, monkeypatch, caplog, browsers: str | None, entry: str | None, reasons: set[str]
) -> None:
    # B5-fix5 item 6 (O5-8): the line said both why a '..' part cannot be placed and why Playwright's browsers are
    # reported, whatever it named.
    linux_links.cwd = None
    if browsers is not None:
        linux_links.os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    _controller_runs_from(monkeypatch, import_path=(*_IMAGE_IMPORT_PATH, *([entry] if entry else [])))
    env = {"HOME": "/data/cli-home"}

    with caplog.at_level(logging.WARNING):
        await _launch_on_linux(_linux_settings(), "/data/cli-home", env)

    (line,) = _written(caplog)
    assert ("where a '..' part leads" in line) == ("dotdot" in reasons), line
    assert ("Playwright's driver" in line) == ("browsers" in reasons), line


@pytest.fixture
def removed_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This process's working directory, removed under it, on the real kernel: getcwd answers ENOENT, nothing can be
    made by a relative name, and a '..' still leads to what was its parent (B5-fix2 item 7's measure)."""
    if not sys.platform.startswith("linux"):
        pytest.skip("a working directory in use cannot be removed on Windows, and the sandbox is Linux's")
    gone = tmp_path / "release-1"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    with pytest.raises(FileNotFoundError):
        os.getcwd()
    return gone


@pytest.mark.parametrize(
    "path, entry, browsers, user_base, named",
    [
        (None, None, None, None, None),
        ("bin:/usr/bin", None, None, None, None),
        (None, "../controller", None, None, "../controller"),
        (None, None, "pw", None, "pw"),
        (None, None, None, "ub", None),
        (None, None, None, "../ub", "../ub"),
    ],
    ids=[
        "this process's own absolute layout",
        "a relative PATH entry",
        "a relative import entry with a '..'",
        "a relative PLAYWRIGHT_BROWSERS_PATH, which the driver resolved at its start",
        "a relative PYTHONUSERBASE",
        "a relative PYTHONUSERBASE with a '..'",
    ],
)
async def test_a_place_read_from_a_working_directory_that_was_removed_is_judged_where_it_can_be(
    data,
    removed_cwd,
    monkeypatch,
    caplog,
    path: str | None,
    entry: str | None,
    browsers: str | None,
    user_base: str | None,
    named: str | None,
) -> None:
    # B5-fix2 item 7 (OC2-8): a release directory removed under a running controller (a deploy that swaps it). Every
    # absolute place is judged as before; a relative one with no '..' leads where nothing can be made, and is not
    # judged; one with a '..' leads past the removed directory, where it cannot be said: reported, and refused under
    # CLI_SANDBOX_REQUIRED=true. Before, every CLI call raised FileNotFoundError.
    # B5-fix4 item 4 (O4-3): but for where Playwright's driver finds the browsers, which it resolved under the
    # directory it was started from, and which a CLI that may write that directory's parent can make again.
    _probe_says(monkeypatch, None)
    if path is not None:
        monkeypatch.setenv("PATH", path)
    if entry is not None:
        monkeypatch.setattr(sys, "path", [entry, *sys.path])
    if browsers is not None:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", browsers)
    if user_base is not None:  # site's own reading of PYTHONUSERBASE, as at a start
        monkeypatch.setenv("PYTHONUSERBASE", user_base)
        monkeypatch.setattr(site, "USER_BASE", None)
        monkeypatch.setattr(site, "getuserbase", _SITE_GETUSERBASE)

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data), data)

    assert command == _under_the_launcher(data)
    if named is None:
        assert _written(caplog) == []
        assert await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true"), data)
        return
    (line,) = _written(caplog)
    assert line.startswith(f"the controller reads {named!r}, at its start or at a call, from its working directory,")
    assert "which was removed" in line
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true"), data)


@pytest.mark.parametrize(
    "where, written, named",
    [
        ("the image's own layout", None, None),
        ("import path", "", None),
        ("import path", "lib", None),
        ("import path", "../controller", "../controller"),
        ("PATH", "bin:/usr/bin", None),
        ("PATH", ":/usr/bin", None),
        ("PATH", "../bin:/usr/bin", "../bin"),
        ("env_file", ".env", None),
        ("env_file", "../settings.env", "../settings.env"),
        ("verifier", "bin/uv run judge/score.py", None),
        ("verifier", "../bin/uv run", "../bin/uv"),
        ("verifier", "uv run ../judge/score.py", "../judge/score.py"),
        ("PLAYWRIGHT_BROWSERS_PATH", "ms-playwright", "ms-playwright"),
        ("PLAYWRIGHT_BROWSERS_PATH", "../ms-playwright", "../ms-playwright"),
        ("HOME", "", ".cache/ms-playwright"),
        ("HOME", "cache-home", "cache-home/.cache/ms-playwright"),
        ("bytecode prefix", "pyc", None),
        ("bytecode prefix", "../pyc", "../pyc"),
        ("XDG_RUNTIME_DIR", "run", None),
        ("XDG_RUNTIME_DIR", "../run", "../run"),
        ("PYTHONUSERBASE", "ub", None),
        ("PYTHONUSERBASE", "../ub", "../ub"),
        ("PYTHONUSERBASE", "part/../ub", "part/../ub"),
    ],
)
async def test_each_place_read_from_a_removed_working_directory_is_judged_by_whether_it_climbs_out(
    linux_links, monkeypatch, caplog, where: str, written: str | None, named: str | None
) -> None:
    # B5-fix2 item 7 (OC2-8): each place the controller reads by a name it was given, from a working directory that
    # was removed. The image's own layout reads "." from it (uvicorn's --app-dir), and .env (as config.py ships it).
    # B5-fix4 item 4 (O4-3): Playwright's driver, started once at the controller's start, resolved a relative
    # PLAYWRIGHT_BROWSERS_PATH (or its default place, where HOME is relative) under the directory then: reported,
    # '..' or not. B5-fix7 item 6 (O7-5): XDG_RUNTIME_DIR, judged one way, is judged so from there too.
    linux_links.cwd = None
    import_path, pycache_prefix, changed = list(_IMAGE_IMPORT_PATH), None, {}
    env_file = ".env"
    if where == "import path":
        import_path.insert(1, written)
    elif where == "PATH":
        linux_links.os.environ["PATH"] = written
    elif where == "env_file":
        env_file = written
    elif where == "verifier":
        changed["HARNESS_UV_COMMAND"] = written
    elif where in ("PLAYWRIGHT_BROWSERS_PATH", "HOME", "XDG_RUNTIME_DIR"):
        linux_links.os.environ[where] = written
    elif where == "bytecode prefix":
        pycache_prefix = written
    elif where == "PYTHONUSERBASE":
        linux_links.user_base = written  # site's user base, as PYTHONUSERBASE gives it
    _controller_runs_from(monkeypatch, import_path=tuple(import_path), pycache_prefix=pycache_prefix)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": env_file})
    env = {"HOME": "/data/cli-home"}

    with caplog.at_level(logging.WARNING):
        command = await _launch_on_linux(_linux_settings(**changed), "/data/cli-home", env)

    assert _given_to_write(command) == ["/data/cli-home", "/tmp", "/var/tmp", "/dev"]
    required = _linux_settings(CLI_SANDBOX_REQUIRED="true", **changed)
    if named is None:
        assert _written(caplog) == []
        assert await _launch_on_linux(required, "/data/cli-home", env)
        return
    (line,) = _written(caplog)
    assert (
        repr(named) in line.partition(", at its start or at a call, from its working directory, which was removed")[0]
    )
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch_on_linux(required, "/data/cli-home", env)


@pytest.mark.parametrize(
    "env_file, home, named",
    [
        ("~/config/controller.env", "/home/controller/config", "/home/controller/config/controller.env"),
        ("/opt/alias/slot/../settings.env", "/data/cli-home", "/opt/alias/slot/../settings.env"),
        (frozenset({"one.env"}), "/data/cli-home", "/tmp/run/one.env"),
        ("", "/data/cli-home", None),
    ],
    ids=["a '~'", "a '..' after a link", "a set of names", "an empty name, which is no file"],
)
async def test_an_env_file_is_judged_where_settings_reads_it(
    linux_links, monkeypatch, caplog, env_file: object, home: str, named: str | None
) -> None:
    # B5-fix2 item 4 (C2-5, S2-10g): pydantic-settings 2.16.2 takes a str or PathLike env_file as one file and anything
    # else as several, and reads each as Path(name).expanduser() from the working directory, the kernel following a
    # link before a '..'. A CLI_HOME in the controller's home is the CLI's own, but for what the controller reads there.
    linux_links.cwd = "/tmp/run"
    linux_links.dirs.update({"/opt/alias", "/data/cli-home/sub", home})
    linux_links.links["/opt/alias/slot"] = "/data/cli-home/sub"
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": env_file})

    await _reads_on_linux(caplog, named, home)


@pytest.mark.parametrize("cwd", ["/app", None], ids=["from a working directory", "from one that was removed"])
@pytest.mark.parametrize(
    "writable, named",
    [(None, None), ("/srv/ub", "/srv/ub")],
    ids=["where it is normalised to no place a CLI may write", "where it is normalised to one"],
)
async def test_an_absolute_user_base_is_judged_where_site_reads_its_user_site(
    linux_links, monkeypatch, caplog, cwd: str | None, writable: str | None, named: str | None
) -> None:
    # site.makepath normalises an absolute user base before it reads the user site in it: /srv/link/../ub is read at
    # /srv/ub, and not after the link, which leads into the CLI home; from a working directory that was removed too.
    linux_links.cwd = cwd
    linux_links.user_base = "/srv/link/../ub"
    linux_links.dirs.update({"/srv/ub", "/data/cli-home/sub"})
    linux_links.links["/srv/link"] = "/data/cli-home/sub"
    _controller_runs_from(monkeypatch, import_path=tuple(entry for entry in _IMAGE_IMPORT_PATH if entry != "."))

    await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE=writable)


@pytest.mark.parametrize(
    "entry, named",
    [("/usr/local/lib/node_modules", None), ("/usr/lib/python3/dist-packages/cache", "/usr/lib/python3/dist-packages")],
    ids=["a directory elsewhere in /usr", "a directory in its packages"],
)
async def test_a_distributions_python_is_judged_by_its_code_and_not_by_the_whole_of_usr(
    linux_links, monkeypatch, caplog, entry: str, named: str | None
) -> None:
    # Debian's python3, without a venv: sys.prefix and sys.base_prefix are /usr. A CLI that updates itself writes
    # /usr/local/lib/node_modules, where the controller has no code.
    linux_links.dirs.add(entry)
    _controller_runs_from(
        monkeypatch,
        launcher="/opt/auto-browser/controller/app/cli_sandbox_launcher.py",
        executable="/usr/bin/python3",
        prefix="/usr",
        base_prefix="/usr",
        stdlib="/usr/lib/python3.11",
        site_packages="/usr/local/lib/python3.11/dist-packages",
        import_path=(
            "/opt/auto-browser/controller",
            "/usr/lib/python311.zip",
            "/usr/lib/python3.11",
            "/usr/lib/python3.11/lib-dynload",
            "/usr/local/lib/python3.11/dist-packages",
            "/usr/lib/python3/dist-packages",
        ),
    )

    command = await _reads_on_linux(caplog, named, CLI_SANDBOX_WRITABLE=entry)

    assert entry in _given_to_write(command)


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
    assert "CLI_HOME" in line and repr(str(home)) in line and repr(str(target)) in line


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
    assert repr(str(data.controller_home)) in line and "next start" in line
    # What it may run is what Python or ssh reads there, and the line says what to keep apart, the blank CLI_HOME's
    # HOME and a TMPDIR among it.
    assert (
        "may run or use at its next start, CLI call, OCR, tunnel or verification, where it is a Python user site, an "
        "ssh config, its own code, a program it runs, or what the verifier's command names, a script it runs or a "
        "file it reads or writes" in line
    )
    assert "TMPDIR" in line and "its HOME where CLI_HOME is blank" in line
    (refusal,) = _written(caplog, logging.ERROR)
    assert repr(str(data.controller_home)) in refusal


@pytest.mark.parametrize("entry", ["its .ssh", "its Python user site", "an ordinary directory"])
async def test_a_directory_a_cli_may_write_inside_the_controllers_home_is_reported_as_the_home_is(
    data, monkeypatch, caplog, entry: str
) -> None:
    # An ssh config, a usercustomize.py: what the controller reads in its home at its next start lies in some
    # directory of it, and the CLI writes that directory.
    _probe_says(monkeypatch, None)
    place = {
        "its .ssh": data.controller_home / ".ssh",
        "its Python user site": data.controller_home / ".local" / "lib" / "python3.12" / "site-packages",
        "an ordinary directory": data.controller_home / "work",
    }[entry]
    changed = {"CLI_SANDBOX_WRITABLE": _directories(place)[0]}

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, **changed), data)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data)

    assert _given_to_write(command) == [str(data.home), str(place)]
    (line,) = _written(caplog)
    assert repr(str(place)) in line and repr(str(data.controller_home)) in line
    assert "next start" in line


@pytest.mark.parametrize(
    "where", ["a directory of its own", "the home's .ssh", "in Python's user base", "what holds Python's user base"]
)
async def test_a_cli_home_in_the_controllers_home_is_its_own_unless_it_meets_what_the_controller_reads_there(
    data, monkeypatch, caplog, where: str
) -> None:
    # A host run whose CLI_HOME cannot be made in /data falls back to a private directory in the user's home, and a
    # CLI_HOME set there is the CLI's. One that is, holds or lies in the home's .ssh or Python's user base is not.
    _probe_says(monkeypatch, None)
    home = {
        "a directory of its own": data.controller_home / ".cache" / "auto-browser" / "cli-home",
        "the home's .ssh": data.controller_home / ".ssh",
        "in Python's user base": data.controller_home / ".local" / "lib",
        "what holds Python's user base": data.controller_home / "python",
    }[where]
    if where == "what holds Python's user base":  # PYTHONUSERBASE set to a directory of it
        monkeypatch.setattr(site, "getuserbase", lambda: str(home / "base"))
    _directories(home)
    env, changed = {**CLI_ENV, "HOME": str(home)}, {"CLI_HOME": str(home)}  # HOME as cli_environment sets it

    with caplog.at_level(logging.WARNING):
        command = await _launch(_settings(data, **changed), data, home=home, env=env)

    assert _given_to_write(command) == [str(home)]
    if where == "a directory of its own":
        assert _written(caplog) == []
        assert await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data, home=home, env=env)
        return
    (line,) = _written(caplog)
    # The place it meets is named, quoted, and lies in the controller's home: its text as the quotes hold it.
    assert repr(str(home)) in line and repr(str(data.controller_home))[1:-1] in line
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data, home=home, env=env)


@pytest.mark.parametrize("which", ["the one HOME names", "the passwd entry's"])
async def test_where_home_and_the_passwd_entry_name_two_homes_a_directory_in_either_is_reported(
    data, monkeypatch, caplog, which: str
) -> None:
    # ssh reads the passwd entry's home, and the controller's next start may have another HOME.
    _probe_says(monkeypatch, None)
    passwd_home = Path(_directories(data.root.parent / "homes" / "passwd")[0])
    monkeypatch.setattr(cli_sandbox, "_passwd_home", lambda: str(passwd_home))
    home = data.controller_home if which == "the one HOME names" else passwd_home
    changed = {"CLI_SANDBOX_WRITABLE": _directories(home / ".ssh")[0]}

    with caplog.at_level(logging.WARNING):
        await _launch(_settings(data, **changed), data)
    with pytest.raises(cli_sandbox.SandboxRequired):
        await _launch(_settings(data, CLI_SANDBOX_REQUIRED="true", **changed), data)

    (line,) = _written(caplog)
    assert repr(str(home / ".ssh")) in line and repr(str(home)) in line


def test_the_passwd_home_is_the_entrys_and_blank_where_there_is_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
    entries = {4242: SimpleNamespace(pw_dir=str(tmp_path))}
    monkeypatch.setattr(cli_sandbox, "pwd", SimpleNamespace(getpwuid=entries.__getitem__))
    assert cli_sandbox._passwd_home() == str(tmp_path)

    monkeypatch.setattr(cli_sandbox, "pwd", SimpleNamespace(getpwuid={}.__getitem__))  # a uid with no entry
    assert cli_sandbox._passwd_home() == ""

    monkeypatch.setattr(cli_sandbox, "pwd", None)  # not POSIX
    assert cli_sandbox._passwd_home() == ""


async def test_where_the_controllers_home_and_a_secret_are_both_open_the_home_is_named(
    data, monkeypatch, caplog
) -> None:
    _probe_says(monkeypatch, None)
    settings = _settings(data, CLI_SANDBOX_WRITABLE=str(data.controller_home), AUTH_ROOT=str(data.outside / "auth"))

    with caplog.at_level(logging.WARNING):
        await _launch(settings, data)

    (line,) = _written(caplog)
    assert repr(str(data.controller_home)) in line and "AUTH_ROOT" not in line


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

        @answers_mcp_list
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
