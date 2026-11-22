"""The controller's Codex calls start no MCP server and keep off each feature the look reads: each call first looks at
what `codex <the call's -c flags> features list` prints and stops where it shows on a feature the look reads,
then asks Codex which servers it would start (`codex <the call's -c flags> mcp list --json`), each with the call's
settings, in the exec's folder, with its environment, to its deadline (through run_cli), then runs `codex exec` with
the project root pinned to that directory and each listed server turned off in one inline table. Where the list shows
a server on, it runs once more with that table, and a server it still shows on stops the call. Each command of a call
carries the call's settings as its -c values, in order, and no other but the server table, on the second list and the
exec. A look or a list that fails or prints no list makes no exec, and what it printed on stdout goes into no error
and no log. app/codex_config.py's settings are the ones the tests write out, and the host bridge's copy agrees.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.codex_config import features_on, listed_mcp_servers
from app.config import Settings
from app.providers.base import CLIResult, ProviderAPIError
from app.providers.openai_adapter import OpenAIAdapter
from tests._codex_bridge import BRIDGE_SCRIPT, load_bridge
from tests._codex_mcp_list import (
    BLANK_LINES_AND_CRLF,
    FIRST_WORD_NO_NAME,
    LOOKED_AT,
    NAMES,
    NEWER_FEATURES_LIST,
    NOT_UTF8_ROW,
    PINNED_FEATURES_LIST,
    SETTINGS,
    config_values,
    features_list,
    is_features_list,
    is_mcp_list,
    listing,
    mcp_list_json,
    turned_off,
)

LISTING = ["mcp", "list", "--json"]
LOOK = ["features", "list"]
DECISION = '{"action":"done","reason":"x","risk_category":"read"}'
# What a list that is not one can print, with a marker the log must not show.
NOT_A_LIST = {
    "nothing": "",
    "not JSON": "Name  Command  sentinel-6c1e\nhome  python3",
    "an object": json.dumps({"name": "home", "note": "sentinel-6c1e"}),
    "a name that is not a string": json.dumps([{"name": 7}]),
    "an entry without a name": json.dumps([{"enabled": True}]),
    "an entry that is not an object": json.dumps(["home"]),
}
# What a features list that is not one can print: a line that is no row among rows, with a marker the log must not
# show.
_HALF = len(PINNED_FEATURES_LIST.splitlines()) // 2
NOT_A_FEATURES_LIST = {
    "nothing": "",
    "a row that ends in neither word": "shell_tool  stable  sentinel-6c1e\n" + PINNED_FEATURES_LIST,
    "a line of two words": "sentinel_6c1e  false\n" + PINNED_FEATURES_LIST,
    "an error among the rows": "\n".join(
        [*PINNED_FEATURES_LIST.splitlines()[:_HALF], "Error: failed", *PINNED_FEATURES_LIST.splitlines()[_HALF:]]
    ),
    "the last row cut short": PINNED_FEATURES_LIST + "shell_tool  stable",
    "JSON": json.dumps({"shell_tool": False, "note": "sentinel-6c1e"}),
}
# The look's refusal, written out: for shell_tool alone, for apps and shell_tool, and for apps, shell_tool and
# codex_hooks (in the order of the call's settings).
ONE_FEATURE_ON = (
    "the Codex configuration turns on the feature shell_tool, which a decision call needs off: turn it off where the "
    "configuration turns it on, such as in a selected profile, in the managed configuration, in a requirements file, "
    "or under an older name in a [features] table"
)
TWO_FEATURES_ON = (
    "the Codex configuration turns on the features apps, shell_tool, which a decision call needs off: turn them off "
    "where the configuration turns them on, such as in a selected profile, in the managed configuration, in a "
    "requirements file, or under an older name in a [features] table"
)
THREE_FEATURES_ON = (
    "the Codex configuration turns on the features apps, shell_tool, codex_hooks, which a decision call needs off: "
    "turn them off where the configuration turns them on, such as in a selected profile, in the managed "
    "configuration, in a requirements file, or under an older name in a [features] table"
)
# For every feature the look reads, in the order of the call's settings.
EVERY_FEATURE_ON = (
    "the Codex configuration turns on the features multi_agent, apps, shell_tool, codex_hooks, shell_snapshot, "
    "js_repl, code_mode, apply_patch_freeform, artifact, image_generation, request_permissions_tool, which a "
    "decision call needs off: turn them off where the configuration turns them on, such as in a selected profile, in "
    "the managed configuration, in a requirements file, or under an older name in a [features] table"
)
NO_FEATURES_LIST = "codex features list printed no list of features"
NO_SERVER_LIST = "codex mcp list printed no list of MCP servers"
# The refusal of a call whose second list still shows a server on, written out.
SERVERS_STILL_ON = (
    "the Codex configuration keeps on MCP servers that a decision call turns off, so the Codex call was not made; "
    "the controller's log names them"
)
# A server the managed configuration keeps on (its enabled = true outranks the call's table), and the marker for where
# its name is shown.
KEPT_ON = "managed-sentinel-6c1e"
# A value a list prints in a server's env, which the log must not show.
LISTED_VALUE = "value-7f3a"
# What a run that exits non-zero printed on stdout alone, with a marker the error and the log must not show.
FAILED_STDOUT = json.dumps({"token": "sentinel-6c1e"})
# Where the adapter's clock starts in the test of the call's deadline.
_MOMENT = 1000.0


@pytest.fixture
def adapter(tmp_path: Path) -> OpenAIAdapter:
    home = tmp_path / "cli-home"
    home.mkdir()
    settings = Settings(_env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=str(home))
    return OpenAIAdapter(settings)


def _printing(stdout: str):
    return lambda command: CLIResult(command=command, stdout=stdout, stderr="", returncode=0)


def _child(stdout: str, stderr: str = "", code: int = 0) -> str:
    """A stand-in run's Python code: it prints ``stdout`` and ``stderr`` and exits with ``code``."""
    return f"import sys; sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({code})"


@pytest.fixture
def through_run_cli(tmp_path: Path, monkeypatch):
    """``decide(**codes)``: one Codex call through the real run_cli, each of its commands a Python child started in its
    place. The look finds every feature of the call off, the list shows "home" on and the second list shows it off,
    but where ``codes`` names the kind (look, list, list_with_the_table); (what the call raised or returned, the kind
    of each command started)."""
    home = tmp_path / "run-cli-home"
    home.mkdir()
    settings = Settings(
        _env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=str(home), CLI_SANDBOX_ENABLED=False
    )
    adapter = OpenAIAdapter(settings)
    real = asyncio.create_subprocess_exec

    async def decide(**codes: str):
        stand_ins = {
            "look": _child(PINNED_FEATURES_LIST),
            "list": _child(mcp_list_json(["home"])),
            "list with the table": _child(mcp_list_json(["home"], off=["home"])),
            "exec": _child(""),
            **{kind.replace("_", " "): code for kind, code in codes.items()},
        }
        started: list[str] = []

        async def spawn(*args, **kwargs):
            started.append(_kind(list(args)))
            return await real(sys.executable, "-c", stand_ins[started[-1]], **kwargs)

        monkeypatch.setattr("app.providers.base.asyncio.create_subprocess_exec", spawn)
        try:
            outcome = await adapter._decide(
                goal="g", observation={"url": "https://example.com"}, context_hints=None, previous_steps=[],
                model_override=None,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - returned for the assertions
            outcome = exc
        return outcome, started

    return decide


async def _decide(adapter: OpenAIAdapter, list_answer, look_answer=_printing(PINNED_FEATURES_LIST)):
    """One Codex call whose `mcp list --json` gets ``list_answer(command)`` and whose `features list` gets
    ``look_answer(command)`` (each a CLIResult, or raises); (what the call returned or raised, each run_cli call's
    keyword arguments)."""
    calls: list[dict] = []

    async def fake_run_cli(**kwargs):
        calls.append(kwargs)
        command = kwargs["command"]
        if is_features_list(command):
            return look_answer(command)
        if is_mcp_list(command):
            return list_answer(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(DECISION, encoding="utf-8")
        return CLIResult(command=command, stdout="", stderr="", returncode=0)

    with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
        try:
            outcome = await adapter._decide(
                goal="g", observation={"url": "https://example.com"}, context_hints=None, previous_steps=[],
                model_override=None,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - returned for the assertions
            outcome = exc
    return outcome, calls


def _exec(calls: list[dict]) -> dict:
    (call,) = [call for call in calls if "exec" in call["command"]]
    return call


def _kind(command: list[str]) -> str:
    if is_features_list(command):
        return "look"
    if is_mcp_list(command):
        return "list with the table" if turned_off(command) else "list"
    return "exec"


def _kinds(calls: list[dict]) -> list[str]:
    """Each run of the call, in order: the look, a list (with the table, or without), or the exec."""
    return [_kind(call["command"]) for call in calls]


async def test_each_server_codex_lists_is_turned_off_in_one_inline_table(adapter) -> None:
    outcome, calls = await _decide(adapter, listing(NAMES))

    assert outcome.decision.action == "done"
    values = config_values(_exec(calls)["command"])
    assert "project_root_markers=[]" in values
    assert "mcp_servers={}" not in values
    tables = [value for value in values if value.startswith("mcp_servers")]
    assert len(tables) == 1, tables
    # Each name a TOML basic string that Codex reads back as the name it listed.
    assert tomllib.loads(tables[0]) == {"mcp_servers": {name: {"enabled": False} for name in NAMES}}


async def test_the_look_and_the_lists_run_as_the_exec_runs(adapter, monkeypatch) -> None:
    # The adapter's clock moves on a second at each read, from one moment: the call's deadline is its first read plus
    # MODEL_REQUEST_TIMEOUT_SECONDS, and a deadline read afresh for a run would be a later one.
    reads = itertools.count()
    monkeypatch.setattr("app.providers.openai_adapter.time", SimpleNamespace(monotonic=lambda: _MOMENT + next(reads)))
    _outcome, calls = await _decide(adapter, listing(["home"]))

    assert _kinds(calls) == ["look", "list", "list with the table", "exec"], calls
    look, listed, relisted, execution = calls
    exec_values = config_values(execution["command"])
    flags = [word for value in exec_values if not value.startswith("mcp_servers") for word in ("-c", value)]
    (table,) = [value for value in exec_values if value.startswith("mcp_servers")]
    assert look["command"] == ["codex", *flags, *LOOK]
    assert listed["command"] == ["codex", *flags, *LISTING]
    assert relisted["command"] == ["codex", *flags, "-c", table, *LISTING]  # the table the exec carries
    command = execution["command"]
    for run in (look, listed, relisted):
        # The same directory as its --cd and the exec's own working directory, and, like the exec, no environment
        # of its own passed to run_cli.
        assert run["cwd"] == execution["cwd"] == command[command.index("--cd") + 1]
        assert run.get("env") == execution.get("env")
        assert run.get("input_text") is None
        # One deadline for the call's runs.
        assert run["deadline"] == execution["deadline"] == _MOMENT + adapter.settings.model_request_timeout_seconds


def test_the_controllers_settings_are_the_settings_written_out_in_the_tests() -> None:
    from app import codex_config

    assert codex_config.CONFIG_OVERRIDES == SETTINGS
    assert codex_config.LOOKED_AT_FEATURES == LOOKED_AT


async def test_each_codex_command_line_carries_the_settings_in_order_and_no_other_value_but_the_table(adapter) -> None:
    _outcome, calls = await _decide(adapter, listing(["home"]))

    assert _kinds(calls) == ["look", "list", "list with the table", "exec"], calls
    for call, table_count in zip(calls, (0, 0, 1, 1), strict=True):
        values = config_values(call["command"])
        tables = [value for value in values if value.startswith("mcp_servers=")]
        assert len(tables) == table_count, call["command"]
        assert values == [*SETTINGS, *tables], call["command"]


@pytest.mark.parametrize(
    ("names", "model"),
    [(["home"], None), (["home"], "gpt-test"), ([], None)],
    ids=["a server", "a server and a model", "no server"],
)
async def test_the_execs_command_line_is_written_out_whole(adapter, monkeypatch, names, model) -> None:
    # Word for word, so that a word outside its -c pairs (another flag, a long --config) is one too many.
    monkeypatch.setattr(adapter.settings, "openai_cli_model", model)
    _outcome, calls = await _decide(adapter, listing(names))

    command = _exec(calls)["command"]
    folder = Path(command[command.index("--cd") + 1])
    assert command == [
        "codex",
        *(["--model", "gpt-test"] if model else []),
        *(word for value in SETTINGS for word in ("-c", value)),
        *(["-c", 'mcp_servers={"home"={enabled=false}}'] if names else []),
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--cd",
        str(folder),
        "--ephemeral",
        "--output-schema",
        str(folder / "browser_action_schema.json"),
        "--output-last-message",
        str(folder / "decision.json"),
        "-",
    ]


async def test_no_server_listed_means_one_list_and_no_table(adapter) -> None:
    outcome, calls = await _decide(adapter, _printing("[]"))

    assert outcome.decision.action == "done"
    assert _kinds(calls) == ["look", "list", "exec"]
    values = config_values(_exec(calls)["command"])
    assert "project_root_markers=[]" in values
    assert [value for value in values if value.startswith("mcp_servers")] == []


async def test_every_server_listed_off_means_one_list(adapter) -> None:
    # As Codex lists them where /etc/codex/requirements.toml turns every server off.
    outcome, calls = await _decide(adapter, _printing(mcp_list_json(["home", "mgd"], off=["home", "mgd"])))

    assert outcome.decision.action == "done"
    assert _kinds(calls) == ["look", "list", "exec"]
    assert turned_off(_exec(calls)["command"]) == {"home", "mgd"}


async def test_a_server_listed_on_means_a_second_list_with_the_table_and_one_exec(adapter) -> None:
    outcome, calls = await _decide(adapter, listing(["home", "other"]))

    assert outcome.decision.action == "done"
    assert _kinds(calls) == ["look", "list", "list with the table", "exec"]
    assert turned_off(calls[2]["command"]) == turned_off(_exec(calls)["command"]) == {"home", "other"}


async def test_a_second_list_that_still_shows_a_server_on_makes_no_exec(adapter, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome, calls = await _decide(adapter, listing(["home", KEPT_ON], kept_on=[KEPT_ON], value=LISTED_VALUE))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (SERVERS_STILL_ON, False)
    assert _kinds(calls) == ["look", "list", "list with the table"]
    assert "sentinel" not in str(outcome)
    # Its name is in the log, where the operator reads it: of what a list prints, the one thing logged.
    assert f"'{KEPT_ON}'" in caplog.text
    assert "'home'" not in caplog.text  # a server the table turned off is not named
    assert LISTED_VALUE not in caplog.text


async def test_a_first_list_with_a_server_off_and_one_on_means_a_second_list_with_every_name(adapter) -> None:
    # home listed off, and a server the managed configuration keeps on listed on, at both lists.
    outcome, calls = await _decide(adapter, _printing(mcp_list_json(["home", KEPT_ON], off=["home"])))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (SERVERS_STILL_ON, False)
    assert _kinds(calls) == ["look", "list", "list with the table"], "codex exec ran"
    assert turned_off(calls[2]["command"]) == {"home", KEPT_ON}  # the second list's table: every name listed


async def test_a_first_list_with_a_server_off_and_one_on_gives_the_exec_a_table_with_every_name(adapter) -> None:
    # home listed off and other on at the first list; the second names other alone, off: the exec's table holds each
    # name the first list had.
    first, second = mcp_list_json(["home", "other"], off=["home"]), mcp_list_json(["other"], off=["other"])

    def lists(command):
        return _printing(second if turned_off(command) else first)(command)

    outcome, calls = await _decide(adapter, lists)

    assert outcome.decision.action == "done"
    assert _kinds(calls) == ["look", "list", "list with the table", "exec"]
    assert turned_off(calls[2]["command"]) == turned_off(_exec(calls)["command"]) == {"home", "other"}


@pytest.mark.parametrize(
    "entry",
    [
        {"name": KEPT_ON},
        {"name": KEPT_ON, "enabled": None},
        {"name": KEPT_ON, "enabled": "false"},
        {"name": KEPT_ON, "enabled": 0},
        {"name": KEPT_ON, "enabled": []},
        {"name": KEPT_ON, "enabled": {}},
    ],
    ids=["no key", "null", "a string", "0", "a list", "an object"],
)
async def test_a_server_whose_enabled_is_not_the_json_false_counts_as_on(adapter, entry) -> None:
    assert listed_mcp_servers(json.dumps([entry])) == {KEPT_ON: True}
    assert listed_mcp_servers(json.dumps([{"name": KEPT_ON, "enabled": False}])) == {KEPT_ON: False}

    outcome, calls = await _decide(adapter, _printing(json.dumps([entry])))

    # The second list, printing the same, still shows it on.
    assert isinstance(outcome, ProviderAPIError), outcome
    assert outcome.message == SERVERS_STILL_ON
    assert _kinds(calls) == ["look", "list", "list with the table"]


@pytest.mark.parametrize("on_first", [False, True], ids=["the entry off first", "the entry on first"])
def test_a_name_listed_twice_is_on_where_one_of_its_entries_is(on_first: bool) -> None:
    entries = [{"name": "home", "enabled": False}, {"name": "home", "enabled": True}]
    listed = json.dumps(entries[::-1] if on_first else entries)

    assert listed_mcp_servers(listed) == {"home": True}


async def test_a_list_that_exits_non_zero_makes_no_exec(adapter) -> None:
    # run_cli's answer to a CLI that exits non-zero, as Codex does on a config.toml it cannot read or parse.
    failed = ProviderAPIError(provider="openai", message="Error: failed to load configuration", retryable=False)

    def fail(command):
        raise failed

    outcome, calls = await _decide(adapter, fail)

    assert outcome is failed
    assert [call for call in calls if "exec" in call["command"]] == []


def _adapters_records(caplog) -> list[str]:
    """Each record of the adapter's logger, and of a logger under it, as its message."""
    return [record.getMessage() for record in caplog.records if record.name.startswith(OpenAIAdapter.__module__)]


def _only_its_length_logged(refusal: str, stdout: str, caplog) -> None:
    """What a look or a list printed is in no log record, and how long it was is: the adapter's one record, written
    out here."""
    assert "sentinel" not in caplog.text
    assert _adapters_records(caplog) == [
        f"{refusal}, so the Codex call was not made; what it printed, {len(stdout)} characters, is not logged"
    ]


@pytest.mark.parametrize("stdout", NOT_A_LIST.values(), ids=NOT_A_LIST.keys())
async def test_a_list_that_prints_no_list_makes_no_exec(adapter, stdout, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome, calls = await _decide(adapter, _printing(stdout))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (NO_SERVER_LIST, False)
    assert [call for call in calls if "exec" in call["command"]] == []
    assert "sentinel" not in str(outcome)
    _only_its_length_logged(NO_SERVER_LIST, stdout, caplog)


def _second_list(answer):
    """A list answer: a server on at the first list, and ``answer(command)`` at the second."""
    return lambda command: answer(command) if turned_off(command) else listing(["home"])(command)


async def test_a_second_list_that_exits_non_zero_makes_no_exec(adapter) -> None:
    failed = ProviderAPIError(provider="openai", message="Error: failed to load configuration", retryable=False)

    def fail(command):
        raise failed

    outcome, calls = await _decide(adapter, _second_list(fail))

    assert outcome is failed
    assert _kinds(calls) == ["look", "list", "list with the table"]


async def test_a_server_only_the_second_list_names_on_makes_no_exec(adapter) -> None:
    outcome, calls = await _decide(adapter, _second_list(_printing(mcp_list_json(["home", "late"], off=["home"]))))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (SERVERS_STILL_ON, False)
    assert _kinds(calls) == ["look", "list", "list with the table"], "codex exec ran"


@pytest.mark.parametrize("stdout", NOT_A_LIST.values(), ids=NOT_A_LIST.keys())
async def test_a_second_list_that_prints_no_list_makes_no_exec(adapter, stdout, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome, calls = await _decide(adapter, _second_list(_printing(stdout)))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (NO_SERVER_LIST, False)
    assert _kinds(calls) == ["look", "list", "list with the table"]
    _only_its_length_logged(NO_SERVER_LIST, stdout, caplog)


@pytest.mark.parametrize(
    ("kind", "started"),
    [("look", ["look"]), ("list", ["look", "list"]), ("list_with_the_table", ["look", "list", "list with the table"])],
    ids=["the look", "the list", "the second list"],
)
async def test_a_run_before_the_exec_that_exits_non_zero_puts_nothing_of_its_stdout_in_the_error_or_the_log(
    through_run_cli, kind, started, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome, kinds = await through_run_cli(**{kind: _child(FAILED_STDOUT, code=1)})

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == ("CLI exited with code 1", False)  # stderr empty: the exit code
    assert "sentinel" not in str(outcome)
    assert "sentinel" not in caplog.text
    assert kinds == started


@pytest.mark.parametrize("kind", ["look", "list", "list_with_the_table"])
async def test_a_run_before_the_exec_that_exits_non_zero_has_codexs_stderr_as_its_error(through_run_cli, kind) -> None:
    error = "Error: failed to load configuration"
    outcome, _kinds = await through_run_cli(**{kind: _child(FAILED_STDOUT, error + "\n", code=1)})

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (error, False)


@pytest.mark.parametrize("feature", LOOKED_AT)
async def test_a_feature_of_the_call_listed_on_makes_no_list_and_no_exec(adapter, feature, caplog) -> None:
    with caplog.at_level(logging.ERROR, "app.providers.openai_adapter"):
        outcome, calls = await _decide(adapter, _printing("[]"), _printing(features_list([feature])))

    # ONE_FEATURE_ON is the refusal written out for shell_tool: the same sentence, for this feature.
    expected = ONE_FEATURE_ON.replace("shell_tool", feature)
    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (expected, False)
    assert _kinds(calls) == ["look"]
    assert expected in caplog.text


@pytest.mark.parametrize(
    ("on", "said"),
    [(["shell_tool"], ONE_FEATURE_ON), (["shell_tool", "apps"], TWO_FEATURES_ON), (LOOKED_AT, EVERY_FEATURE_ON)],
    ids=["one", "two", "every one"],
)
async def test_the_error_says_where_a_configuration_can_turn_a_feature_on(adapter, on, said, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome, _calls = await _decide(adapter, _printing("[]"), _printing(features_list(on)))

    assert outcome.message == said
    assert _adapters_records(caplog) == [f"{said}; the Codex call was not made"]  # logged whole


async def test_the_error_names_each_feature_listed_on_and_nothing_else_codex_printed(adapter, caplog) -> None:
    listed = features_list(["codex_hooks", "fast_mode", "shell_tool", "apps"]) + "sentinel_6c1e  stable  true\n"
    with caplog.at_level(logging.DEBUG, "app.providers.openai_adapter"):
        outcome, _calls = await _decide(adapter, _printing("[]"), _printing(listed))

    assert outcome.message == THREE_FEATURES_ON  # in the call's own order
    assert "sentinel" not in caplog.text
    assert "fast_mode" not in outcome.message


@pytest.mark.parametrize(
    "listed",
    [
        PINNED_FEATURES_LIST,
        NEWER_FEATURES_LIST,
        features_list(["unified_exec"]),
        "".join(row + "\n" for row in PINNED_FEATURES_LIST.splitlines() if not row.startswith("codex_hooks ")),
        BLANK_LINES_AND_CRLF,
    ],
    ids=["the pinned Codex", "a newer Codex", "unified_exec on", "no codex_hooks row", "blank lines and CRLF"],
)
async def test_a_features_list_with_each_feature_of_the_call_off_or_missing_goes_on(adapter, listed) -> None:
    outcome, calls = await _decide(adapter, _printing("[]"), _printing(listed))

    assert outcome.decision.action == "done"
    assert _kinds(calls) == ["look", "list", "exec"]


@pytest.mark.parametrize("stdout", NOT_A_FEATURES_LIST.values(), ids=NOT_A_FEATURES_LIST.keys())
async def test_a_look_that_prints_no_features_list_makes_no_list_and_no_exec(adapter, stdout, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome, calls = await _decide(adapter, _printing("[]"), _printing(stdout))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (NO_FEATURES_LIST, False)
    assert "sentinel" not in str(outcome)
    assert _kinds(calls) == ["look"]
    _only_its_length_logged(NO_FEATURES_LIST, stdout, caplog)  # as an unreadable server list's


@pytest.mark.parametrize("stdout", FIRST_WORD_NO_NAME.values(), ids=FIRST_WORD_NO_NAME.keys())
async def test_a_line_whose_first_word_is_no_feature_name_makes_no_list_and_no_exec(adapter, stdout) -> None:
    outcome, calls = await _decide(adapter, _printing("[]"), _printing(stdout))

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (NO_FEATURES_LIST, False)
    assert _kinds(calls) == ["look"]


async def test_a_name_with_a_byte_that_is_no_utf8_makes_no_list_and_no_exec(through_run_cli) -> None:
    # Through the real run_cli, whose decode puts U+FFFD in the name.
    outcome, kinds = await through_run_cli(look=f"import sys; sys.stdout.buffer.write({NOT_UTF8_ROW!r})")

    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (NO_FEATURES_LIST, False)
    assert kinds == ["look"]


async def test_a_look_that_exits_non_zero_makes_no_list_and_no_exec(adapter) -> None:
    failed = ProviderAPIError(provider="openai", message="Error: failed to load configuration", retryable=False)

    def fail(command):
        raise failed

    outcome, calls = await _decide(adapter, _printing("[]"), fail)

    assert outcome is failed
    assert _kinds(calls) == ["look"]


def test_the_look_reads_every_feature_the_call_turns_off_but_unified_exec() -> None:
    every = features_list(LOOKED_AT + ("unified_exec",))
    assert features_on(every) == list(LOOKED_AT)
    assert features_on(features_list(["unified_exec"])) == []


@pytest.mark.parametrize("true_first", [False, True], ids=["the false row first", "the true row first"])
def test_of_two_rows_for_one_feature_the_true_one_wins(true_first: bool) -> None:
    true_row = "shell_tool  stable  true\n"  # beside the pinned list's own row of shell_tool, which reads false
    listed = true_row + PINNED_FEATURES_LIST if true_first else PINNED_FEATURES_LIST + true_row

    assert features_on(listed) == ["shell_tool"]


def test_a_feature_added_to_the_calls_settings_is_looked_at(tmp_path) -> None:
    from app import codex_config

    source = Path(codex_config.__file__).read_text(encoding="utf-8")
    added = source.replace(
        '    "features.multi_agent=false",\n', '    "features.multi_agent=false",\n    "features.planted=false",\n', 1
    )
    assert added != source
    copy = tmp_path / "codex_config_copy.py"
    copy.write_text(added, encoding="utf-8")
    module = _load(copy)

    assert "planted" in module.LOOKED_AT_FEATURES
    assert module.features_on(PINNED_FEATURES_LIST + "planted  stable  true\n") == ["planted"]
    assert module.features_on(PINNED_FEATURES_LIST + "planted  stable  false\n") == []


def _load(path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not BRIDGE_SCRIPT.is_file(), reason="scripts/ is not shipped in the controller image")
def test_the_host_bridges_copy_agrees() -> None:
    from app import codex_config

    bridge = load_bridge("codex_host_bridge_mcp_copy_under_test")
    assert bridge.MCP_LIST_COMMAND == codex_config.MCP_LIST_COMMAND
    assert bridge.FEATURES_LIST_COMMAND == codex_config.FEATURES_LIST_COMMAND
    assert bridge.CONFIG_OVERRIDES == codex_config.CONFIG_OVERRIDES
    assert bridge.CONFIG_FLAGS == codex_config.CONFIG_FLAGS
    assert bridge.FEATURE_NOT_LOOKED_AT == codex_config.FEATURE_NOT_LOOKED_AT
    assert bridge.LOOKED_AT_FEATURES == codex_config.LOOKED_AT_FEATURES
    for name in NAMES:
        assert bridge.toml_basic_string(name) == codex_config.toml_basic_string(name)
    assert bridge.mcp_servers_off(NAMES) == codex_config.mcp_servers_off(NAMES)
    entries = [{"name": "a"}, {"name": "b", "enabled": False}, {"name": "c", "enabled": "false"}]
    entries += [{"name": name, "enabled": enabled} for name, enabled in (("d", 0), ("e", []), ("f", {}), ("g", None))]
    twice = [{"name": "h", "enabled": False}, {"name": "h", "enabled": True}]
    listed = (mcp_list_json(NAMES), mcp_list_json(NAMES, off=NAMES[1:]), json.dumps(entries), "[]")
    for output in (*listed, json.dumps(twice), json.dumps(twice[::-1])):
        assert bridge.listed_mcp_servers(output.encode("utf-8")) == codex_config.listed_mcp_servers(output)
    for output in NOT_A_LIST.values():
        assert bridge.listed_mcp_servers(output.encode("utf-8")) is codex_config.listed_mcp_servers(output) is None
    looks = (
        PINNED_FEATURES_LIST,
        NEWER_FEATURES_LIST,
        BLANK_LINES_AND_CRLF,
        features_list(LOOKED_AT + ("unified_exec",)),
        features_list(["shell_tool"], listed=NEWER_FEATURES_LIST),
        *NOT_A_FEATURES_LIST.values(),
    )
    for output in looks:
        assert bridge.features_on(output.encode("utf-8")) == codex_config.features_on(output)
    for output in FIRST_WORD_NO_NAME.values():
        assert bridge.features_on(output.encode("utf-8")) is codex_config.features_on(output) is None
    assert bridge.features_on(NOT_UTF8_ROW) is codex_config.features_on(NOT_UTF8_ROW.decode("utf-8", "replace")) is None
    assert bridge.FEATURE_NAME.pattern == codex_config.FEATURE_NAME.pattern
    for names in (["shell_tool"], ["apps", "shell_tool"]):
        assert bridge.features_on_error(names) == codex_config.features_on_error(names)


# A name for each control character (U+0000 to U+001F, and U+007F): a TOML basic string takes none of them as it is
# but the tab.
CONTROL_NAMES = [f"a{chr(code)}b" for code in (*range(0x20), 0x7F)]


@pytest.mark.parametrize("copy", ["controller", "bridge"])
def test_a_name_that_holds_any_control_character_reads_back_as_itself(copy: str) -> None:
    from app import codex_config

    if copy == "bridge" and not BRIDGE_SCRIPT.is_file():
        pytest.skip("scripts/ is not shipped in the controller image")
    module = codex_config if copy == "controller" else load_bridge("codex_host_bridge_toml_copy_under_test")
    for name in CONTROL_NAMES:
        assert tomllib.loads("name = " + module.toml_basic_string(name)) == {"name": name}, repr(name)
