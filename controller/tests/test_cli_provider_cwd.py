"""A provider CLI child must not start in the controller's checkout, where a .env would hand it the secrets."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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


def _private_env(tmp: str) -> dict[str, str]:
    """HOME / XDG / LOCALAPPDATA all inside tmp, so a test never touches the real home."""
    return {"HOME": tmp, "USERPROFILE": tmp, "LOCALAPPDATA": tmp, "CLI_HOME": ""}


def _adapter() -> ClaudeAdapter:
    return ClaudeAdapter(Settings(_env_file=None))


async def test_without_a_cli_home_the_child_starts_in_a_private_per_user_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        cwd = await _spawn_cwd(_adapter())

        assert cwd is not None
        folder = Path(cwd)
        assert folder.is_dir()
        assert Path(tmp) in folder.parents, "the folder lives under the user's own home, not the shared temp dir"
        assert not (folder / ".env").exists()
        if os.name == "posix":
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
            assert folder == Path(tmp) / ".cache" / "auto-browser" / "cli-home"


@pytest.mark.skipif(os.name != "posix", reason="XDG_RUNTIME_DIR is a POSIX convention")
async def test_xdg_runtime_dir_is_used_when_it_is_the_users_own() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Path(tmp) / "run"
        runtime.mkdir(mode=0o700)
        with patch.dict(os.environ, {**_private_env(tmp), "XDG_RUNTIME_DIR": str(runtime)}, clear=True):
            cwd = await _spawn_cwd(_adapter())

        assert cwd == str(runtime / "auto-browser-cli")


async def test_a_private_directory_removed_between_two_calls_is_recreated() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        adapter = _adapter()
        first = adapter.effective_cli_home()
        shutil.rmtree(first)

        second = adapter.effective_cli_home()

        assert second == first
        assert Path(second).is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(Path(second).stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_a_symlink_in_place_of_the_private_directory_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        adapter = _adapter()
        folder = Path(adapter.effective_cli_home())
        folder.rmdir()
        target = Path(tmp) / "elsewhere"
        target.mkdir(mode=0o700)
        folder.symlink_to(target)

        with pytest.raises(RuntimeError, match=str(folder)):
            adapter.effective_cli_home()


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_a_private_directory_with_the_wrong_mode_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        adapter = _adapter()
        folder = Path(adapter.effective_cli_home())
        folder.chmod(0o755)

        with pytest.raises(RuntimeError, match=str(folder)):
            adapter.effective_cli_home()


async def test_a_caller_given_cwd_is_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = GeminiAdapter(Settings(_env_file=None, CLI_HOME=str(Path(tmp) / "home")))

        assert await _spawn_cwd(adapter, cwd=tmp) == tmp
