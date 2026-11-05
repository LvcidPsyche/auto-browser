"""A provider CLI child must not start in the controller's checkout, where a .env would hand it the secrets."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.providers.base import CLIResult
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter

# What the one error says, wherever the private CLI home failed; never a server path.
UNUSABLE = "set CLI_HOME to a writable directory"


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


def _home_cache(tmp: str) -> Path:
    if os.name == "nt":
        return Path(tmp) / "auto-browser" / "cli-home"
    return Path(tmp) / ".cache" / "auto-browser" / "cli-home"


async def test_without_a_cli_home_the_child_starts_in_a_private_per_user_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        cwd = await _spawn_cwd(_adapter())

        assert cwd is not None
        folder = Path(cwd)
        assert folder.is_dir()
        assert Path(tmp) in folder.parents, "the folder lives under the user's own home, not the shared temp dir"
        assert folder == _home_cache(tmp)
        if os.name == "posix":
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700


async def test_the_private_directory_holds_empty_env_sentinels() -> None:
    # The Gemini CLI reads the first .env it finds from its working directory up (then the user's home): empty ones
    # here stop it before it reaches a parent's.
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        folder = Path(_adapter().effective_cli_home())

        assert (folder / ".env").read_bytes() == b""
        assert (folder / ".gemini" / ".env").read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="XDG_RUNTIME_DIR is a POSIX convention")
async def test_xdg_runtime_dir_is_used_when_it_is_the_users_own() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Path(tmp) / "run"
        runtime.mkdir(mode=0o700)
        with patch.dict(os.environ, {**_private_env(tmp), "XDG_RUNTIME_DIR": str(runtime)}, clear=True):
            cwd = await _spawn_cwd(_adapter())

        assert cwd == str(runtime / "auto-browser-cli")


@pytest.mark.skipif(os.name != "posix", reason="XDG_RUNTIME_DIR is a POSIX convention")
async def test_a_relative_xdg_runtime_dir_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "run").mkdir(mode=0o700)
        monkeypatch.chdir(tmp)  # where a relative value would resolve: the controller's working directory
        with patch.dict(os.environ, {**_private_env(tmp), "XDG_RUNTIME_DIR": "run"}, clear=True):
            cwd = await _spawn_cwd(_adapter())

        assert cwd == str(_home_cache(tmp))


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_a_refused_xdg_candidate_falls_through_to_the_home_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Path(tmp) / "run"
        runtime.mkdir(mode=0o700)
        (runtime / "auto-browser-cli").mkdir(mode=0o700)
        (runtime / "auto-browser-cli").chmod(0o755)
        with patch.dict(os.environ, {**_private_env(tmp), "XDG_RUNTIME_DIR": str(runtime)}, clear=True):
            cwd = await _spawn_cwd(_adapter())

        assert cwd == str(_home_cache(tmp))


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_an_ancestor_others_can_write_refuses_the_candidate(caplog: pytest.LogCaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        shared = _home_cache(tmp).parent
        shared.parent.mkdir(mode=0o700)
        shared.mkdir()
        shared.chmod(0o777)  # anyone could replace the private directory's name in it

        with caplog.at_level(logging.WARNING, "app.providers.base"), pytest.raises(RuntimeError) as caught:
            _adapter().effective_cli_home()

        assert UNUSABLE in str(caught.value)
        assert tmp not in str(caught.value)
        assert re.search(re.escape(str(shared)), caplog.text)


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_a_sticky_ancestor_others_can_write_is_accepted() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        shared = _home_cache(tmp).parent
        shared.parent.mkdir(mode=0o700)
        shared.mkdir()
        shared.chmod(0o1777)  # as /tmp: others may add names to it, not replace this user's

        assert _adapter().effective_cli_home() == str(_home_cache(tmp))


THIS_USER = 4242
OTHER_USER = 4343


@contextlib.contextmanager
def _owners(tmp: str, owned_by: dict[str, int]):
    """Run as THIS_USER on a tree whose directories under `tmp` belong to THIS_USER unless `owned_by` names another
    owner for a relative path, and everything outside it to root. The real owner of a test directory is whoever runs
    the suite (root in the controller image), so the owners are faked: os.stat and os.lstat report them and getuid
    answers THIS_USER, which keeps the test meaningful as root and as an ordinary user."""
    root = os.path.realpath(tmp)
    real_stat, real_lstat = os.stat, os.lstat

    def faked(real):
        def call(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            location = os.path.abspath(os.fspath(path))
            if location != root and not location.startswith(root + os.sep):
                owner = 0
            else:
                owner = owned_by.get(os.path.relpath(location, root).replace(os.sep, "/"), THIS_USER)
            return os.stat_result((*tuple(info)[:4], owner, *tuple(info)[5:10]))

        return call

    with (
        patch("app.providers.base.os.getuid", return_value=THIS_USER),
        patch("app.providers.base.os.stat", faked(real_stat)),
        patch("app.providers.base.os.lstat", faked(real_lstat)),
    ):
        yield


def _make_cache_tree(tmp: str) -> None:
    cache = _home_cache(tmp).parent.parent
    cache.mkdir(mode=0o755)
    _home_cache(tmp).parent.mkdir(mode=0o755)


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_an_ancestor_another_user_owns_refuses_the_candidate(caplog: pytest.LogCaptureFixture) -> None:
    # A .cache planted by someone else (0755, theirs): not writable by others, but they could swap the directories
    # inside it, and the CLIs read config (CLAUDE.md, .env) from the parents of their working directory.
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        _make_cache_tree(tmp)

        with (
            _owners(tmp, {".cache": OTHER_USER}),
            caplog.at_level(logging.WARNING, "app.providers.base"),
            pytest.raises(RuntimeError) as caught,
        ):
            _adapter().effective_cli_home()

        assert UNUSABLE in str(caught.value)
        assert tmp not in str(caught.value)
        assert re.search(re.escape(os.path.join(tmp, ".cache")), caplog.text)
        assert f"owned by uid {OTHER_USER}" in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_ancestors_this_user_owns_are_accepted_up_to_the_first_root_owned_one() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        _make_cache_tree(tmp)

        with _owners(tmp, {}):
            assert _adapter().effective_cli_home() == str(_home_cache(tmp))


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_the_walk_stops_at_the_first_root_owned_ancestor() -> None:
    # Root owns .cache here, so what is above it (another user's tmp) is not this directory's business.
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        _make_cache_tree(tmp)

        with _owners(tmp, {".cache": 0, ".": OTHER_USER}):
            assert _adapter().effective_cli_home() == str(_home_cache(tmp))


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
async def test_a_symlink_in_place_of_the_private_directory_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        adapter = _adapter()
        folder = Path(adapter.effective_cli_home())
        shutil.rmtree(folder)
        target = Path(tmp) / "elsewhere"
        target.mkdir(mode=0o700)
        folder.symlink_to(target)

        with caplog.at_level(logging.WARNING, "app.providers.base"), pytest.raises(RuntimeError) as caught:
            adapter.effective_cli_home()

        assert UNUSABLE in str(caught.value)
        assert str(folder) not in str(caught.value)
        assert re.search(re.escape(str(folder)), caplog.text)


@pytest.mark.skipif(os.name != "posix", reason="owner and mode checks are POSIX")
async def test_a_private_directory_with_the_wrong_mode_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        adapter = _adapter()
        folder = Path(adapter.effective_cli_home())
        folder.chmod(0o755)

        with caplog.at_level(logging.WARNING, "app.providers.base"), pytest.raises(RuntimeError) as caught:
            adapter.effective_cli_home()

        assert UNUSABLE in str(caught.value)
        assert str(folder) not in str(caught.value)
        assert re.search(re.escape(str(folder)), caplog.text)
        assert "0755" in caplog.text


async def test_no_home_raises_the_one_error() -> None:
    environ = {"CLI_HOME": ""}  # no HOME, USERPROFILE, LOCALAPPDATA or XDG_RUNTIME_DIR
    with (
        patch.dict(os.environ, environ, clear=True),
        patch("app.providers.base.Path.home", side_effect=RuntimeError("Could not determine home directory.")),
        pytest.raises(RuntimeError) as caught,
    ):
        _adapter().effective_cli_home()

    assert UNUSABLE in str(caught.value)
    assert "Could not determine" not in str(caught.value)


@pytest.mark.parametrize("cli_home", ["", "unusable"])
async def test_a_home_that_cannot_hold_the_directory_raises_the_one_error(cli_home: str) -> None:
    # A file where the home should be: no directory can be made under it, for root as for anyone.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home-is-a-file"
        home.write_text("x", encoding="utf-8")
        environ = {**_private_env(str(home)), "CLI_HOME": str(home / "cli-home") if cli_home else ""}
        with patch.dict(os.environ, environ, clear=True), pytest.raises(RuntimeError) as caught:
            _adapter().effective_cli_home()

    assert UNUSABLE in str(caught.value)
    assert str(home) not in str(caught.value)


async def test_the_codex_per_call_directory_is_made_under_the_cli_home() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _private_env(tmp), clear=True):
        adapter = OpenAIAdapter(Settings(_env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex"))
        seen: list[str | None] = []

        async def fake_run_cli(*, command, input_text=None, env=None, cwd=None):
            seen.append(cwd)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text('{"action":"done","reason":"x","risk_category":"read"}', encoding="utf-8")
            return CLIResult(command=command, stdout="", stderr="", returncode=0)

        with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
            await adapter._decide(
                goal="g", observation={"url": "https://x.test/"}, context_hints=None, previous_steps=[],
                model_override=None,
            )  # fmt: skip

        assert seen and seen[0] is not None
        assert Path(seen[0]).parent == Path(adapter.effective_cli_home())


async def test_a_caller_given_cwd_is_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = GeminiAdapter(Settings(_env_file=None, CLI_HOME=str(Path(tmp) / "home")))

        assert await _spawn_cwd(adapter, cwd=tmp) == tmp
