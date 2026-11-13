"""Whether a provider CLI starts under the Landlock launcher, and what the controller does where it cannot (N1 and
its decision 5): app/cli_sandbox.py, and run_cli, the one place that starts a CLI.

The decisions are tested anywhere, with the probe stood in for. What a CLI that run_cli started can then open is
tested on Linux where Landlock works, as in test_cli_sandbox_launcher.py.
"""

from __future__ import annotations

import asyncio
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
from tests.test_cli_sandbox_launcher import STAND_IN, needs_landlock
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
    monkeypatch.setattr(cli_sandbox, "_probed", [])
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
    return SimpleNamespace(root=root, home=home, shot=shots[0], other_shot=shots[1], outside=root.parent / "outside")


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
    """Stand in for the probe: it answers ``answer`` (None: the launcher works), and each run's environment is kept."""
    runs: list[dict[str, str]] = []

    def probe(env: dict[str, str]) -> str | None:
        runs.append(env)
        return answer

    monkeypatch.setattr(cli_sandbox, "_probe", probe)
    return runs


async def _launch(settings: Settings, data: SimpleNamespace, *, home: Path | None = None, image: Path | None = None):
    return await cli_sandbox.launch_command(
        settings,
        COMMAND,
        cli_home=str(home or data.home),
        image_path=str(image) if image else None,
        env=CLI_ENV,
    )


def _under_the_launcher(data: SimpleNamespace, *grants: str) -> list[str]:
    protect = ["--protect", str(data.root), "--rw", str(data.home)]
    return [sys.executable, "-I", "-S", cli_sandbox.LAUNCHER, *protect, *grants, "--", *COMMAND]


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

    def slow_probe(env: dict[str, str]) -> None:
        runs.append(env)
        time.sleep(0.2)

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


# ---- the probe ----


def _the_probe_runs(monkeypatch: pytest.MonkeyPatch, outcome) -> list[SimpleNamespace]:
    """Stand in for subprocess.run under the probe, on what is taken for Linux: ``outcome`` is what it returns, or
    the error it raises. Each call is kept."""
    calls: list[SimpleNamespace] = []

    def run(command, **kwargs):
        calls.append(SimpleNamespace(command=command, **kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cli_sandbox, "_LINUX", True)
    monkeypatch.setattr(cli_sandbox.subprocess, "run", run)
    return calls


def test_the_probe_runs_the_launcher_itself_in_its_probe_mode(monkeypatch) -> None:
    calls = _the_probe_runs(monkeypatch, subprocess.CompletedProcess([], 0, b"", b""))

    assert cli_sandbox._probe(CLI_ENV) is None

    (call,) = calls
    probe = [sys.executable, "-I", "-S", cli_sandbox.LAUNCHER, "--protect", cli_sandbox.PROTECTED_ROOT, "--probe"]
    assert call.command == probe
    assert call.env == CLI_ENV and call.stdin == subprocess.DEVNULL
    assert call.timeout == cli_sandbox.PROBE_TIMEOUT_SECONDS


def test_a_probe_that_exits_with_the_launchers_status_is_a_reason_that_holds_its_line(monkeypatch) -> None:
    line = MARKER + "landlock_create_ruleset(version): ENOSYS (Function not implemented)\n"
    _the_probe_runs(monkeypatch, subprocess.CompletedProcess([], EXIT_NOT_APPLIED, b"", line.encode()))

    reason = cli_sandbox._probe(CLI_ENV)

    assert reason is not None and "ENOSYS" in reason and str(EXIT_NOT_APPLIED) in reason
    assert reason.splitlines() == [reason], "one line of the log"


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired(["python"], 30), FileNotFoundError(2, "No such file or directory"), OSError(12, "x")],
    ids=["no answer in time", "no python", "no memory"],
)
def test_a_probe_that_cannot_be_run_is_a_reason_and_not_an_error(monkeypatch, failure: Exception) -> None:
    _the_probe_runs(monkeypatch, failure)

    reason = cli_sandbox._probe(CLI_ENV)

    assert reason and reason.splitlines() == [reason]


def test_off_linux_the_probe_starts_nothing(monkeypatch) -> None:
    calls = _the_probe_runs(monkeypatch, AssertionError("started a process"))
    monkeypatch.setattr(cli_sandbox, "_LINUX", False)

    assert cli_sandbox._probe(CLI_ENV)
    assert calls == []


def test_this_process_takes_linux_for_linux_and_nothing_else() -> None:
    assert cli_sandbox._LINUX is sys.platform.startswith("linux")


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

    def held_probe(env: dict[str, str]) -> None:
        probing.set()
        answered.wait(30)

    monkeypatch.setattr(cli_sandbox, "_probe", held_probe)
    adapter = ClaudeAdapter(_settings(data))

    with _spawning(_child()) as spawn:
        task = asyncio.create_task(adapter.run_cli(command=COMMAND, env=CLI_ENV))
        while not probing.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        answered.set()
        while not cli_sandbox._probed:
            await asyncio.sleep(0.01)

    spawn.assert_not_awaited()
    assert cli_sandbox._probed == [None], "the probe ran to its end, and the next call does not probe again"


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
    assert cli_sandbox._probe(CLI_ENV) is None


@needs_landlock
def test_the_probe_passes_for_the_root_production_protects() -> None:
    # /data is there in the image and is not on a runner: the ruleset is applied whole either way.
    assert cli_sandbox.PROTECTED_ROOT == "/data"

    assert cli_sandbox._probe(CLI_ENV) is None


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
