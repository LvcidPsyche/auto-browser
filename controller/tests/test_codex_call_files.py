"""The files of a Codex call's working directory, which the CLI can write and the controller reads and writes outside
the CLI sandbox (FXN1 U-3): decision.json is taken only as a regular file that is itself there, and the schema file
is made, never written through what is already at its name.

The link and FIFO cases need POSIX: a symbolic link takes a privilege on Windows, which has no FIFO and no O_NOFOLLOW.
"""

from __future__ import annotations

import errno
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.providers import openai_adapter
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


async def test_a_calls_directory_is_removed_after_it(adapter) -> None:
    await _decide(adapter, lambda path: path.write_text(DECISION, encoding="utf-8"))

    assert list(Path(adapter.settings.cli_home).iterdir()) == []


@pytest.mark.skipif(
    not hasattr(os, "O_PATH") or os.geteuid() == 0, reason="needs Linux's O_PATH, and a user who may not list any dir"
)
async def test_a_cli_home_the_controller_may_write_but_not_list_takes_a_calls_directory(adapter) -> None:
    home = Path(adapter.settings.cli_home)
    home.chmod(0o333)
    try:
        result = await _decide(adapter, lambda path: path.write_text(DECISION, encoding="utf-8"))
    finally:
        home.chmod(0o700)

    assert result.decision.reason == "from the file"
    assert list(home.iterdir()) == []


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


async def test_a_decision_file_far_past_the_stdout_limit_is_read_no_further_than_the_limit(adapter) -> None:
    # The file is read on the event loop: one byte past the limit says it is too large, and the rest is never read.
    read = 0
    real_read = os.read

    def counted(fd: int, size: int) -> bytes:
        nonlocal read
        chunk = real_read(fd, size)
        read += len(chunk)
        return chunk

    with patch.object(os, "read", counted), pytest.raises(RuntimeError) as caught:
        await _decide(adapter, lambda path: path.write_bytes(b" " * (8 * CLI_STDOUT_LIMIT_BYTES)))

    assert str(caught.value).startswith(REFUSED)
    assert read <= CLI_STDOUT_LIMIT_BYTES + 1, read


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


def _swap_for_a_link(directory: Path, to: Path) -> None:
    """What a CLI working in the home meanwhile can do: move the call's directory aside, and leave a link at its name."""
    directory.rename(directory.with_name(directory.name + "-aside"))
    directory.symlink_to(to, target_is_directory=True)


async def _outcome(adapter: OpenAIAdapter, leave) -> object:
    """What the call returned, or the error it raised: the assertions after it say which was wrong."""
    try:
        return await _decide(adapter, leave)
    except Exception as exc:
        return exc


@pytest.fixture
def closed(tmp_path: Path) -> Path:
    """A directory the CLI sandbox keeps a CLI out of, holding a decision.json of its own."""
    place = tmp_path / "data" / "auth"
    place.mkdir(parents=True)
    (place / "decision.json").write_text(CLOSED, encoding="utf-8")
    return place


@posix_only
async def test_a_call_directory_swapped_for_a_link_once_made_is_not_written_read_or_removed_through_it(
    adapter, closed: Path, caplog
) -> None:
    made = adapter._call_directory

    def call_directory():
        directory = made()
        _swap_for_a_link(Path(directory.name), closed)
        return directory

    with patch.object(adapter, "_call_directory", side_effect=call_directory), caplog.at_level(logging.WARNING):
        result = await _outcome(adapter, lambda path: None)

    assert sorted(path.name for path in closed.iterdir()) == ["decision.json"], "the schema was made through the link"
    assert (closed / "decision.json").read_text(encoding="utf-8") == CLOSED
    assert getattr(result, "raw_text", result) == STDOUT, "the decision was read through the link"
    (line,) = _warnings(caplog)
    assert "not removed" in line


@posix_only
async def test_a_call_directory_swapped_for_a_link_while_the_cli_runs_gives_no_decision_through_it(
    adapter, closed: Path
) -> None:
    def swap(decision: Path) -> None:
        _swap_for_a_link(decision.parent, closed)

    with patch.object(adapter, "parse_decision_text", wraps=adapter.parse_decision_text) as parsed:
        await _outcome(adapter, swap)

    assert [entry.args[0] for entry in parsed.call_args_list] == [STDOUT], "the decision was read through the link"
    assert (closed / "decision.json").read_text(encoding="utf-8") == CLOSED


@pytest.mark.skipif(not openai_adapter._BY_DESCRIPTOR, reason="a call's directory is made by descriptor only on POSIX")
def test_a_call_directory_that_cannot_be_opened_once_made_is_removed(adapter) -> None:
    home = Path(adapter.settings.cli_home)
    opened = os.open

    def open_all_but_the_calls_directory(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise OSError(errno.EMFILE, "Too many open files")
        return opened(path, flags, mode, dir_fd=dir_fd)

    with patch("os.open", side_effect=open_all_but_the_calls_directory), pytest.raises(OSError):
        openai_adapter._CallDirectory(str(home))

    assert list(home.iterdir()) == []
