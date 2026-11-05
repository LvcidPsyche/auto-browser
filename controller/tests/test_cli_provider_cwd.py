"""A provider CLI child must not start in the controller's checkout, where a .env would hand it the secrets."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter


async def _spawn_cwd(adapter, **kwargs) -> str | None:
    process = SimpleNamespace(
        communicate=AsyncMock(return_value=(b"ok", b"")),
        returncode=0,
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
        await adapter.run_cli(command=["cli"], **kwargs)
    return spawn.await_args.kwargs["cwd"]


async def test_the_child_starts_in_the_cli_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "cli-home"
        adapter = GeminiAdapter(Settings(_env_file=None, CLI_HOME=str(home)))

        cwd = await _spawn_cwd(adapter)

        assert cwd == str(home)
        assert home.is_dir(), "the CLI home is created when missing"


async def test_without_a_cli_home_the_child_starts_in_an_empty_directory_under_the_temp_dir() -> None:
    adapter = ClaudeAdapter(Settings(_env_file=None, CLI_HOME=""))

    cwd = await _spawn_cwd(adapter)

    assert cwd is not None
    folder = Path(cwd)
    assert folder.is_dir()
    assert folder.parent == Path(tempfile.gettempdir())
    assert not (folder / ".env").exists()


async def test_a_caller_given_cwd_is_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = GeminiAdapter(Settings(_env_file=None, CLI_HOME=str(Path(tmp) / "home")))

        assert await _spawn_cwd(adapter, cwd=tmp) == tmp
