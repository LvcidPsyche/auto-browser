"""Files that hold secrets are written 0600, and a rewrite never widens a file.

atomic_write_text chmod-ed every file it wrote to 0666 & ~umask, so a 0600 file
became 0644 on its next save and a new credential file was born world-readable
under the default umask. It now keeps the mode the target already has, takes an
explicit mode=, and the stores that hold credentials, tokens and typed text ask
for 0600.

Windows cannot show a mode wider than the read-only bit, so each test also
records the mode passed to os.chmod (the computed mode); on POSIX the file
itself is checked with os.stat as well.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.agent_jobs import AgentJobStore
from app.approvals import FileApprovalStore
from app.auth_state import AuthStateManager
from app.cron_service import CronService
from app.mcp_transport import McpHttpTransport
from app.models import ApprovalRecord, BrowserActionDecision, SessionRecord
from app.proxy_personas import ProxyPersonaStore
from app.session_store import FileSessionStore
from app.utils import atomic_write_text

POSIX = sys.platform != "win32"
posix_only = pytest.mark.skipif(not POSIX, reason="Windows models only the read-only bit")


@pytest.fixture()
def chmods() -> Iterator[list[tuple[Path, int]]]:
    """Every (path, mode) os.chmod is asked for while the test runs."""
    calls: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def spy(path, mode, *args, **kwargs):
        calls.append((Path(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    with patch("app.utils.os.chmod", spy):
        yield calls


def modes_asked_for(calls: list[tuple[Path, int]], final: Path) -> list[int]:
    """The modes requested for the temp files that became ``final``."""
    return [mode for path, mode in calls if path.parent == final.parent and path.name.startswith(f".{final.name}.")]


def assert_private(calls: list[tuple[Path, int]], final: Path) -> None:
    assert final.exists(), final
    assert modes_asked_for(calls, final) == [0o600]
    if POSIX:
        assert stat.S_IMODE(final.stat().st_mode) == 0o600


# ── atomic_write_text ───────────────────────────────────────────────────────


def test_mode_argument_sets_the_mode_of_a_new_file(tmp_path: Path, chmods) -> None:
    target = tmp_path / "secret.json"

    atomic_write_text(target, "x", mode=0o600)

    assert target.read_text(encoding="utf-8") == "x"
    assert_private(chmods, target)


@posix_only
def test_mode_argument_tightens_a_file_that_was_wider(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)

    atomic_write_text(target, "new", mode=0o600)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_replacing_a_0600_file_keeps_0600_under_the_default_umask(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o600)

    with patch("app.utils._UMASK", 0o022):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_replacing_a_group_readable_file_keeps_its_mode(tmp_path: Path) -> None:
    target = tmp_path / "shared.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    with patch("app.utils._UMASK", 0o077):
        atomic_write_text(target, "new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_an_existing_files_mode_wins_over_the_umask_on_every_platform(tmp_path: Path, chmods) -> None:
    target = tmp_path / "kept.json"
    target.write_text("old", encoding="utf-8")
    if POSIX:
        target.chmod(0o600)
    before = stat.S_IMODE(target.stat().st_mode)

    # Under umask 022 a recomputed mode would be 0644; Windows reports 0666 and
    # POSIX 0600 here, so neither can be the recomputed one.
    with patch("app.utils._UMASK", 0o022):
        atomic_write_text(target, "new")

    assert before != 0o644
    assert modes_asked_for(chmods, target) == [before]


def test_a_0600_target_is_rewritten_0600_even_where_stat_cannot_show_it(tmp_path: Path, chmods) -> None:
    # Windows reports 0666 for every writable file, so this is where the
    # preserved mode is proven there: the target is reported as 0600 and the
    # replacement is asked to be 0600, not recomputed from the umask.
    target = tmp_path / "secret.json"
    target.write_text("old", encoding="utf-8")

    with (
        patch("app.utils.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)),
        patch("app.utils._UMASK", 0o022),
    ):
        atomic_write_text(target, "new")

    assert modes_asked_for(chmods, target) == [0o600]


def test_a_new_file_without_a_mode_gets_what_a_plain_open_would(tmp_path: Path, chmods) -> None:
    target = tmp_path / "public.json"

    with patch("app.utils._UMASK", 0o022):
        atomic_write_text(target, "x")

    assert modes_asked_for(chmods, target) == [0o644]


def test_a_failed_write_leaves_no_temp_file_and_the_old_content(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text("old", encoding="utf-8")

    with patch("app.utils.os.replace", side_effect=OSError("boom")), pytest.raises(OSError):
        atomic_write_text(target, "new", mode=0o600)

    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["secret.json"]


# ── the stores that hold secrets ────────────────────────────────────────────


def test_proxy_persona_file_is_private(tmp_path: Path, chmods) -> None:
    path = tmp_path / "proxy-personas.json"

    ProxyPersonaStore(path).set_persona("us", server="http://proxy.example:8080", username="u", password="hunter2")

    assert_private(chmods, path)


async def test_cron_store_with_webhook_keys_is_private(tmp_path: Path, chmods) -> None:
    path = tmp_path / "cron.json"

    job = await CronService(path).create_job(name="nightly", goal="check", webhook_enabled=True)

    assert job["id"]
    assert json.loads(path.read_text(encoding="utf-8"))[job["id"]]["webhook_key"]
    assert_private(chmods, path)


async def test_encrypted_auth_state_is_private(tmp_path: Path, chmods) -> None:
    class Context:
        async def storage_state(self, path: str | None = None) -> dict:
            return {"cookies": [{"name": "s", "value": "SECRET", "domain": "example.com", "path": "/"}], "origins": []}

    manager = AuthStateManager(encryption_key=Fernet.generate_key().decode(), require_encryption=True, max_age_hours=72)

    await manager.write_storage_state(Context(), tmp_path / "profile" / "auth-state.json")

    assert_private(chmods, tmp_path / "profile" / "auth-state.json.enc")


@posix_only
async def test_plaintext_auth_state_is_private_too(tmp_path: Path) -> None:
    class Context:
        async def storage_state(self, path: str | None = None) -> dict:
            state = {"cookies": [{"name": "s", "value": "SECRET"}], "origins": []}
            if path is not None:
                Path(path).write_text(json.dumps(state), encoding="utf-8")
            return state

    manager = AuthStateManager(encryption_key=None, require_encryption=False, max_age_hours=72)
    target = tmp_path / "profile" / "auth-state.json"

    with patch("app.utils._UMASK", 0o022):
        await manager.write_storage_state(Context(), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


async def test_session_record_is_private(tmp_path: Path, chmods) -> None:
    store = FileSessionStore(tmp_path / "sessions")
    await store.startup()
    record = SessionRecord(
        id="s1",
        name="one",
        created_at="2026-01-01T00:00:01Z",
        updated_at="2026-01-01T00:00:01Z",
        status="active",
        current_url="https://example.com",
        title="Example",
        artifact_dir="/tmp/artifacts",
        takeover_url="http://localhost:6080/?password=abc",
        remote_access={"active": True},
    )

    await store.upsert(record)

    assert_private(chmods, tmp_path / "sessions" / "s1.json")


async def test_approval_record_with_typed_text_is_private(tmp_path: Path, chmods) -> None:
    store = FileApprovalStore(tmp_path / "approvals")
    await store.startup()
    approval = ApprovalRecord(
        id="a1",
        session_id="s1",
        kind="write",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        reason="needs review",
        action=BrowserActionDecision(
            action="type", reason="log in", selector="#password", text="hunter2", risk_category="write"
        ),
    )

    await store.upsert(approval)

    assert_private(chmods, tmp_path / "approvals" / "a1.json")


async def test_agent_job_record_is_private(tmp_path: Path, chmods) -> None:
    store = AgentJobStore(tmp_path / "jobs")
    await store.startup()

    record = await store.create(session_id="s1", kind="agent_run", request={"goal": "log in with hunter2"})

    assert_private(chmods, tmp_path / "jobs" / f"{record.id}.json")


def test_mcp_session_store_is_private(tmp_path: Path, chmods) -> None:
    path = tmp_path / "mcp" / "sessions.json"
    transport = McpHttpTransport(
        tool_gateway=SimpleNamespace(), server_name="auto-browser", server_version="0", session_store_path=str(path)
    )

    transport._persist_sessions()

    assert_private(chmods, path.resolve())
