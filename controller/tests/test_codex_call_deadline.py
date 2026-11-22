"""A Codex call's CLI runs, `codex <its -c flags> features list`, `codex <its -c flags> mcp list --json` (twice where
the first shows a server on) and `codex exec`, share one deadline: the call's MODEL_REQUEST_TIMEOUT_SECONDS, counted
from the start of the look, as in the host bridge. Each run gets what the runs before it left; a run whose deadline
has passed starts no child and fails as a timed-out run does.

The stand-in codex is a patched process start: each run's command is recorded and a Python child started in its place.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.providers.base import ProviderAPIError
from app.providers.openai_adapter import OpenAIAdapter
from tests._codex_mcp_list import PINNED_FEATURES_LIST, is_features_list, is_mcp_list, mcp_list_json, turned_off

TIMEOUT_SECONDS = 5.0
# The hang tests make no exec, so they need no time for one to start: the host bridge's hang test's budget.
HANG_TIMEOUT_SECONDS = 2.0
# A run that answers 2 s after its child starts (starting a child can take a second or more on a loaded Windows host,
# so the exec still starts well before the deadline); a run that does not answer in time.
_SLOW_SECONDS = 2.0
_NO_ANSWER = "import time; time.sleep(60)"
# What the call may take past its timeout (the kill and the drain of a child that holds no other process): less than
# one slow run takes, so a call that gave a later run a whole timeout of its own after a slow one is over the bound.
MARGIN_SECONDS = _SLOW_SECONDS * 3 / 4
# The shared-deadline test's timeout: room for the look and the second list, each slow, and for the exec to start.
SHARED_TIMEOUT_SECONDS = 8.0
# run_cli's message for a run past its deadline, given MODEL_REQUEST_TIMEOUT_SECONDS.
TIMED_OUT = "CLI command timed out after {:.0f}s"


def _printing(stdout: str, after: float = 0) -> str:
    """A stand-in run that prints ``stdout`` ``after`` seconds after its child starts."""
    return f"import sys, time; time.sleep({after}); sys.stdout.write({stdout!r})"


# Each run's stand-in, by kind, unless a test says otherwise: a look that finds every feature of the call off, a list
# that names no server, a second list that shows each server off, an exec that does not answer.
QUICK = {
    "look": _printing(PINNED_FEATURES_LIST),
    "list": _printing("[]"),
    "list with the table": _printing(mcp_list_json(["home"], off=["home"])),
    "exec": _NO_ANSWER,
}


def _kind(command: list[str]) -> str:
    if is_features_list(command):
        return "look"
    if is_mcp_list(command):
        return "list with the table" if turned_off(command) else "list"
    return "exec"


def _adapter(tmp_path: Path, timeout: float) -> OpenAIAdapter:
    home = tmp_path / "cli-home"
    home.mkdir()
    settings = Settings(
        _env_file=None,
        OPENAI_AUTH_MODE="cli",
        OPENAI_CLI_PATH="codex",
        CLI_HOME=str(home),
        CLI_SANDBOX_ENABLED=False,
        MODEL_REQUEST_TIMEOUT_SECONDS=timeout,
    )
    return OpenAIAdapter(settings)


@pytest.fixture
def adapter(tmp_path: Path) -> OpenAIAdapter:
    return _adapter(tmp_path, TIMEOUT_SECONDS)


@pytest.fixture
def stand_in_codex(monkeypatch):
    """``start(**codes)``: until the test ends, each run of a kind ``codes`` names (look, list, list_with_the_table,
    exec) runs that code, and any other the QUICK one. The kinds of the commands started are returned, and the
    children started are ``start.children``; a child still running at the end is killed."""
    real = asyncio.create_subprocess_exec
    started: list[str] = []
    children: list[asyncio.subprocess.Process] = []

    def start(**codes: str):
        stand_ins = {**QUICK, **{kind.replace("_", " "): code for kind, code in codes.items()}}

        async def spawn(*args, **kwargs):
            kind = _kind(list(args))
            started.append(kind)
            child = await real(sys.executable, "-c", stand_ins[kind], **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr("app.providers.base.asyncio.create_subprocess_exec", spawn)
        return started

    start.children = children
    yield start
    for child in children:
        if child.returncode is None:
            child.kill()


async def _decide(adapter: OpenAIAdapter) -> tuple[object, float]:
    began = time.monotonic()
    try:
        outcome = await adapter._decide(
            goal="g", observation={"url": "https://example.com"}, context_hints=None, previous_steps=[],
            model_override=None,
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - returned for the assertions
        outcome = exc
    return outcome, time.monotonic() - began


def _timed_out(outcome: object, timeout: float) -> None:
    assert isinstance(outcome, ProviderAPIError), outcome
    assert (outcome.message, outcome.retryable) == (TIMED_OUT.format(timeout), True)


def _none_left_running(stand_in_codex) -> None:
    """Each child the call started has ended by the time it returned: the run that timed out was killed."""
    left = [child.pid for child in stand_in_codex.children if child.returncode is None]
    assert not left, f"a child of the call was left running: {left}"


async def test_a_slow_list_and_a_slow_exec_end_within_one_timeout(adapter, stand_in_codex) -> None:
    started = stand_in_codex(list=_printing("[]", _SLOW_SECONDS))

    outcome, elapsed = await _decide(adapter)

    _none_left_running(stand_in_codex)
    assert started == ["look", "list", "exec"]
    _timed_out(outcome, TIMEOUT_SECONDS)
    assert elapsed < TIMEOUT_SECONDS + MARGIN_SECONDS, f"the call took {elapsed:.2f}s"


async def test_the_look_the_lists_and_the_exec_share_one_deadline(tmp_path, stand_in_codex) -> None:
    # The look and the second list are slow; the exec, which does not answer, gets what the three left.
    adapter = _adapter(tmp_path, SHARED_TIMEOUT_SECONDS)
    started = stand_in_codex(
        look=_printing(PINNED_FEATURES_LIST, _SLOW_SECONDS),
        list=_printing(mcp_list_json(["home"])),
        list_with_the_table=_printing(mcp_list_json(["home"], off=["home"]), _SLOW_SECONDS),
    )

    outcome, elapsed = await _decide(adapter)

    assert started == ["look", "list", "list with the table", "exec"]
    _timed_out(outcome, SHARED_TIMEOUT_SECONDS)
    assert elapsed < SHARED_TIMEOUT_SECONDS + MARGIN_SECONDS, f"the call took {elapsed:.2f}s"


@pytest.mark.parametrize(
    ("hangs", "started_before"),
    [("look", []), ("list", ["look"]), ("list_with_the_table", ["look", "list"])],
    ids=["the look", "the list", "the second list"],
)
async def test_a_run_that_hangs_before_the_exec_is_killed_at_the_calls_deadline_and_makes_no_exec(
    tmp_path, stand_in_codex, hangs, started_before
) -> None:
    # Each run's own hang test, as the host bridge has them: the run is killed when the call's time is up, and the call
    # fails as a timed-out run does.
    adapter = _adapter(tmp_path, HANG_TIMEOUT_SECONDS)
    started = stand_in_codex(**{"list": _printing(mcp_list_json(["home"])), hangs: _NO_ANSWER})

    outcome, elapsed = await _decide(adapter)

    _none_left_running(stand_in_codex)
    assert started == [*started_before, hangs.replace("_", " ")]
    _timed_out(outcome, HANG_TIMEOUT_SECONDS)
    assert elapsed < HANG_TIMEOUT_SECONDS + MARGIN_SECONDS, f"the call took {elapsed:.2f}s"


async def test_a_run_whose_deadline_has_passed_starts_no_child_and_fails_as_a_timeout(adapter, stand_in_codex) -> None:
    started = stand_in_codex()

    with pytest.raises(ProviderAPIError) as caught:
        await adapter.run_cli(command=["codex", "exec", "-"], input_text="p", deadline=time.monotonic())

    assert started == []
    assert (caught.value.message, caught.value.retryable) == (TIMED_OUT.format(TIMEOUT_SECONDS), True)
