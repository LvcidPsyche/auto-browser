"""The files of a Codex call's working directory, which the CLI can write and the controller reads and writes outside
the CLI sandbox (FXN1 U-3): decision.json is taken only as a regular file that is itself there, and the schema file
is made, never written through what is already at its name.

The link and FIFO cases need POSIX: a symbolic link takes a privilege on Windows, which has no FIFO and no O_NOFOLLOW.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.providers.base import CLI_STDOUT_LIMIT_BYTES, CLIResult
from app.providers.openai_adapter import OpenAIAdapter

DECISION = '{"action":"done","reason":"from the file","risk_category":"read"}'
CLOSED = '{"action":"done","reason":"CONTENT OF A CLOSED FILE","risk_category":"read"}'
STDOUT = '{"action":"done","reason":"from stdout","risk_category":"read"}'
REFUSED = "openai CLI did not return a valid BrowserActionDecision JSON object"
posix_only = pytest.mark.skipif(os.name == "nt", reason="needs POSIX: a link without a privilege, a FIFO, O_NOFOLLOW")


@pytest.fixture
def adapter(tmp_path: Path) -> OpenAIAdapter:
    home = tmp_path / "cli-home"
    home.mkdir()
    return OpenAIAdapter(Settings(_env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=str(home)))


async def _decide(adapter: OpenAIAdapter, leave) -> object:
    """One Codex call whose CLI leaves ``leave(decision_path)`` behind and prints STDOUT."""

    async def fake_run_cli(*, command, **kwargs):
        leave(Path(command[command.index("--output-last-message") + 1]))
        return CLIResult(command=command, stdout=STDOUT, stderr="", returncode=0)

    with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
        return await adapter._decide(
            goal="g",
            observation={"url": "https://example.com", "title": "t", "interactables": []},
            context_hints=None,
            previous_steps=[],
            model_override=None,
        )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.providers.openai_adapter" and record.levelno == logging.WARNING
    ]


async def test_a_regular_decision_file_is_read_as_before(adapter) -> None:
    result = await _decide(adapter, lambda path: path.write_text(DECISION, encoding="utf-8"))

    assert result.decision.reason == "from the file" and result.raw_text == DECISION


async def test_with_no_decision_file_the_reply_is_what_the_cli_printed(adapter) -> None:
    result = await _decide(adapter, lambda path: None)

    assert result.decision.reason == "from stdout" and result.raw_text == STDOUT


@posix_only
async def test_a_decision_file_that_is_a_link_to_a_closed_file_fails_the_call_and_nothing_of_that_file_leaves(
    adapter, tmp_path: Path, caplog
) -> None:
    # A CLI under the sandbox cannot open the file; the controller, which is not under it, must not open it for it.
    closed = tmp_path / "data" / "auth" / "state.json"
    closed.parent.mkdir(parents=True)
    closed.write_text(CLOSED, encoding="utf-8")

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
        await _decide(adapter, lambda path: path.symlink_to(closed))

    assert str(caught.value).startswith(REFUSED)
    assert "CLOSED FILE" not in str(caught.value) and str(tmp_path) not in str(caught.value)
    (line,) = _warnings(caplog)
    assert "a link" in line and "CLOSED FILE" not in line
    assert closed.read_text(encoding="utf-8") == CLOSED


@posix_only
async def test_a_decision_file_that_is_a_fifo_fails_the_call_without_blocking(adapter, caplog) -> None:
    finished = threading.Event()
    fifos: list[Path] = []

    def leave_a_fifo(path: Path) -> None:
        os.mkfifo(path)
        fifos.append(path)

    def unblock() -> None:
        # Where the read blocks (it waits for a writer), write a decision after a while: the test then fails on what
        # the call returned instead of hanging.
        if not finished.wait(5) and fifos:
            with open(fifos[0], "w", encoding="utf-8") as writer:
                writer.write(DECISION)

    helper = threading.Thread(target=unblock, daemon=True)
    helper.start()
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
            await _decide(adapter, leave_a_fifo)
    finally:
        finished.set()
        helper.join(10)

    assert time.monotonic() - started < 5, "the read waited for a writer"
    assert str(caught.value).startswith(REFUSED)
    (line,) = _warnings(caplog)
    assert "a FIFO" in line


async def test_a_decision_file_that_is_a_directory_fails_the_call(adapter, caplog) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
        await _decide(adapter, lambda path: path.mkdir())

    assert str(caught.value).startswith(REFUSED)
    (line,) = _warnings(caplog)
    if os.name != "nt":  # Windows does not open a directory as a file at all
        assert "a directory" in line


async def test_a_decision_file_larger_than_the_stdout_limit_fails_the_call(adapter, caplog) -> None:
    oversize = DECISION + " " * (CLI_STDOUT_LIMIT_BYTES + 1 - len(DECISION))

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
        await _decide(adapter, lambda path: path.write_text(oversize, encoding="utf-8"))

    assert str(caught.value).startswith(REFUSED)
    (line,) = _warnings(caplog)
    assert str(CLI_STDOUT_LIMIT_BYTES) in line


async def test_a_decision_file_of_the_stdout_limit_is_read(adapter) -> None:
    whole = DECISION + " " * (CLI_STDOUT_LIMIT_BYTES - len(DECISION))

    result = await _decide(adapter, lambda path: path.write_text(whole, encoding="utf-8"))

    assert result.decision.reason == "from the file"


@posix_only
async def test_a_link_at_the_schemas_name_fails_the_call_and_its_target_is_untouched(
    adapter, tmp_path: Path, caplog
) -> None:
    # A CLI of a call that runs at the same time can write in this call's directory between its making and the
    # schema's write: the controller makes the schema file, and never writes through what is there.
    target = tmp_path / "data" / "api-token"
    target.parent.mkdir(parents=True)
    target.write_text("the token", encoding="utf-8")
    made = adapter._call_directory

    def call_directory() -> tempfile.TemporaryDirectory[str]:
        directory = made()
        (Path(directory.name) / "browser_action_schema.json").symlink_to(target)
        return directory

    started = AsyncMock()
    with (
        patch.object(adapter, "_call_directory", side_effect=call_directory),
        patch.object(adapter, "run_cli", new=started),
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError) as caught,
    ):
        await adapter._decide(
            goal="g",
            observation={"url": "https://example.com", "title": "t", "interactables": []},
            context_hints=None,
            previous_steps=[],
            model_override=None,
        )

    assert target.read_text(encoding="utf-8") == "the token"
    started.assert_not_awaited()
    assert str(tmp_path) not in str(caught.value)
    assert len(_warnings(caplog)) == 1
