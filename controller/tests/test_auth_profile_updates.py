"""A failed save of an auth profile loses nothing (round 4c, item 2).

Re-saving a profile replaced its state file first and wrote its metadata after, and on a failure deleted
the new state file: the profile's previous login was gone, and its metadata named a state that no longer
existed. A failure inside write_storage_state, after its replace, left the new state behind. An update
that fails at any point now leaves the previous state and metadata as they were, a first save that fails
leaves neither, and a save that succeeds ends with both new. Real AuthStateManager, encrypted and plain,
on temp files; the page and the browser context are fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.auth_state import AuthStateManager
from app.browser.page_provenance import PageProvenance
from app.browser.services import auth_profiles as auth_profiles_module
from app.browser.services.auth_profiles import BrowserAuthProfileService
from app.models import OperatorIdentity
from app.session_ownership import acting_as

PROFILE = "shop"


class FakeContext:
    """What storage_state() returns, or writes when given a path; counts the calls."""

    def __init__(self, cookie: str) -> None:
        self.state = {"cookies": [{"name": "sid", "value": cookie, "domain": "shop.example", "path": "/"}]}
        self.calls = 0

    async def storage_state(self, path: str | None = None) -> dict:
        self.calls += 1
        if path is not None:
            Path(path).write_text(json.dumps(self.state), encoding="utf-8")
        return self.state


class FakePage:
    url = "https://shop.example/account"

    def __init__(self, title_error: Exception | None = None) -> None:
        self.title_error = title_error

    async def title(self) -> str:
        if self.title_error is not None:
            raise self.title_error
        return "Account"


def session_saving(context: FakeContext, page: FakePage | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="s1", context=context, page=page or FakePage(), last_auth_state_path=None, auth_profile_name=None
    )


@pytest.fixture(params=["plain", "encrypted"])
def service(request, tmp_path: Path) -> BrowserAuthProfileService:
    key = Fernet.generate_key().decode() if request.param == "encrypted" else None
    manager = SimpleNamespace(
        settings=SimpleNamespace(auth_root=str(tmp_path), auth_state_encryption_key=key),
        auth_state=AuthStateManager(encryption_key=key, require_encryption=False, max_age_hours=72),
        _page_off_list=lambda session: False,
        _page_refused=lambda page: False,
        _shown_url=lambda page: page.url,
        page_provenance=PageProvenance(),
    )
    return BrowserAuthProfileService(manager)


def profile_files(service: BrowserAuthProfileService) -> dict[str, bytes]:
    """Every file in the profile's directory, by name, with its bytes: temp files would show up here too."""
    directory = Path(service.manager.settings.auth_root) / "profiles" / PROFILE
    if not directory.exists():
        return {}
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()}


async def saved(service: BrowserAuthProfileService, cookie: str) -> dict[str, bytes]:
    await service.save_for_session(session_saving(FakeContext(cookie)), PROFILE)
    return profile_files(service)


def failing_metadata_write(error: BaseException):
    def write(*_args, **_kwargs) -> None:
        raise error

    return write


async def test_an_update_whose_metadata_write_fails_keeps_the_previous_profile(service, monkeypatch) -> None:
    before = await saved(service, "OLD-LOGIN")
    monkeypatch.setattr(auth_profiles_module, "atomic_write_text", failing_metadata_write(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == before


async def test_an_update_whose_state_inspection_fails_keeps_the_previous_profile(service, monkeypatch) -> None:
    """write_storage_state inspects the file it wrote, after its replace, and that was outside the cleanup."""
    before = await saved(service, "OLD-LOGIN")

    def inspect(_path):
        raise OSError("stat failed")

    monkeypatch.setattr(service.manager.auth_state, "inspect", inspect)

    with pytest.raises(OSError, match="stat failed"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == before


async def test_an_update_cancelled_during_its_metadata_write_keeps_the_previous_profile(service, monkeypatch) -> None:
    before = await saved(service, "OLD-LOGIN")
    monkeypatch.setattr(auth_profiles_module, "atomic_write_text", failing_metadata_write(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == before


async def test_an_update_whose_metadata_cannot_be_put_in_place_keeps_the_previous_profile(service, monkeypatch) -> None:
    """The state was moved in first: when the metadata's move fails, the previous state goes back."""
    before = await saved(service, "OLD-LOGIN")
    replace = os.replace

    def refusing_the_metadata(source, destination) -> None:
        if Path(destination).name == "profile.json":
            raise OSError("rename refused")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", refusing_the_metadata)

    with pytest.raises(OSError, match="rename refused"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == before


async def test_a_first_save_whose_metadata_write_fails_leaves_nothing(service, monkeypatch) -> None:
    monkeypatch.setattr(auth_profiles_module, "atomic_write_text", failing_metadata_write(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == {}


async def test_an_update_whose_title_read_fails_keeps_the_state_and_writes_it_once(service) -> None:
    """The title is read before the state is written (B6), so a failed read writes nothing."""
    before = await saved(service, "OLD-LOGIN")
    context = FakeContext("NEW-LOGIN")
    page = FakePage(title_error=RuntimeError("Target page, context or browser has been closed"))

    with pytest.raises(RuntimeError, match="closed"):
        await service.save_for_session(session_saving(context, page), PROFILE)

    assert profile_files(service) == before
    assert context.calls == 0


async def test_control_an_update_that_succeeds_has_the_new_state_and_metadata(service) -> None:
    before = await saved(service, "OLD-LOGIN")

    after = await saved(service, "NEW-LOGIN")

    assert sorted(after) == sorted(before), "a temp file was left behind"
    state_name = next(name for name in after if name.startswith("state.json"))
    assert after[state_name] != before[state_name]
    state = service.manager.auth_state.prepare_for_context(
        Path(service.manager.settings.auth_root) / "profiles" / PROFILE / state_name
    ).storage_state
    cookies = (json.loads(Path(state).read_text(encoding="utf-8")) if isinstance(state, str) else state)["cookies"]
    assert [cookie["value"] for cookie in cookies] == ["NEW-LOGIN"]
    assert json.loads(after["profile.json"])["profile_name"] == PROFILE


# ── A first save leaves no login without an owner (item 35) ──

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")


class Killed(BaseException):
    """The process dying: nothing after it runs, no rollback and no cleanup."""


def second_rename(monkeypatch, error: BaseException) -> None:
    """The second of a save's two renames raises ``error``; the first lands."""
    real, calls = auth_profiles_module.replace_retrying, []

    def rename(source, destination) -> None:
        calls.append(Path(destination).name)
        if len(calls) == 2:
            raise error
        real(source, destination)

    monkeypatch.setattr(auth_profiles_module, "replace_retrying", rename)


def assert_no_state_another_operator_may_open(service: BrowserAuthProfileService) -> None:
    """What the save left: no state, or one whose metadata names alice, so bob is refused it."""
    files = profile_files(service)
    states = [name for name in files if name.startswith("state.json")]
    assert "profile.json" in files or not states, f"a state with no owner: {sorted(files)}"
    with acting_as(BOB):
        if states:
            with pytest.raises(PermissionError):
                service.require_access(PROFILE, action="loading an auth profile")


async def test_a_first_save_killed_between_its_renames_leaves_no_state_without_an_owner(service, monkeypatch) -> None:
    """Item 35 (Fable, copy F2): the state was put in place first, then the metadata. Killed between the two, a
    first save left state.json and no profile.json: no owner, so under named credentials every operator could
    load, export or save over that login."""
    second_rename(monkeypatch, Killed())
    # A dead process rolls nothing back, so nothing is as it was, and nothing after the rollback runs either.
    monkeypatch.setattr(auth_profiles_module, "_roll_back", lambda *_args: False)

    with acting_as(ALICE), pytest.raises(Killed):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert_no_state_another_operator_may_open(service)
    assert [name for name in profile_files(service) if name.startswith("state.json")] == []
    # Metadata naming alice and no state: hidden from bob, and no login to load for alice.
    with acting_as(BOB):
        assert await service.list() == []
        with pytest.raises(PermissionError):
            await service.get(PROFILE)
        with pytest.raises(PermissionError):
            await service.export(PROFILE)
    with acting_as(ALICE):
        [listed] = await service.list()
        assert (listed["metadata"]["owner"], listed["auth_state"]["exists"]) == ("alice", False)
        with pytest.raises(FileNotFoundError, match="No saved auth profile"):
            service.resolve_state_path(PROFILE, must_exist=True)


async def test_a_first_save_whose_second_rename_and_rollback_fail_leaves_no_state_without_an_owner(
    service, monkeypatch
) -> None:
    """Item 35: the second rename fails, and so does the rollback's removal (a file Windows keeps locked)."""
    second_rename(monkeypatch, OSError("rename refused"))
    unlink = Path.unlink

    def locked(path: Path, missing_ok: bool = False) -> None:
        if path.name in {"state.json", "state.json.enc", "profile.json"}:
            raise PermissionError(f"{path.name} is locked")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)

    with acting_as(ALICE), pytest.raises(OSError, match="rename refused"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert_no_state_another_operator_may_open(service)


async def test_control_a_first_save_whose_state_cannot_be_put_in_place_leaves_nothing(service, monkeypatch) -> None:
    replace = os.replace

    def refusing_the_state(source, destination) -> None:
        if Path(destination).name.startswith("state.json"):
            raise OSError("rename refused")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", refusing_the_state)

    with acting_as(ALICE), pytest.raises(OSError, match="rename refused"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == {}


async def test_a_resave_of_an_ownerless_profile_killed_between_its_renames_leaves_no_new_login_without_an_owner(
    service, monkeypatch
) -> None:
    """Item 52 (Fable E-COPY-02): the order was chosen by whether metadata exists, not by whether it names an
    owner. A re-save by a named operator of a profile whose metadata names none (saved under the shared token,
    or before owners) records that operator as owner, but put the state first: killed between the renames, it
    left the new login beside ownerless metadata, which every operator may open under named credentials."""
    before = await saved(service, "OLD-LOGIN")  # no operator: no owner
    second_rename(monkeypatch, Killed())
    monkeypatch.setattr(auth_profiles_module, "_roll_back", lambda *_args: False)

    with acting_as(ALICE), pytest.raises(Killed):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    files = profile_files(service)
    new_logins = [name for name in files if name.startswith("state.json") and files[name] != before.get(name)]
    assert not new_logins or service.owner_of(PROFILE) == "alice", (new_logins, service.owner_of(PROFILE))
    if new_logins:
        assert_no_state_another_operator_may_open(service)


async def test_control_an_ownerless_resave_whose_second_rename_fails_keeps_the_previous_profile(
    service, monkeypatch
) -> None:
    """Item 52: with its metadata first, a failed re-save puts the previous metadata back once the state is."""
    before = await saved(service, "OLD-LOGIN")
    second_rename(monkeypatch, OSError("rename refused"))

    with acting_as(ALICE), pytest.raises(OSError, match="rename refused"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert profile_files(service) == before


async def test_control_a_resave_puts_the_state_in_place_before_the_metadata(service, monkeypatch) -> None:
    """Its metadata already names the owner: the order that keeps the previous login on a failure stays."""
    with acting_as(ALICE):
        await saved(service, "OLD-LOGIN")
    order: list[str] = []
    real = auth_profiles_module.replace_retrying

    def recording(source, destination) -> None:
        order.append(Path(destination).name.split(".")[0])
        real(source, destination)

    monkeypatch.setattr(auth_profiles_module, "replace_retrying", recording)

    with acting_as(ALICE):
        await saved(service, "NEW-LOGIN")

    assert order[:2] == ["state", "profile"]
