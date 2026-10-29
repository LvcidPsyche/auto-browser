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
