"""An auth-profile save never leaves the profile without its state file (round 4d, item 1).

Round 4c's save moved the previous state aside before renaming the new one in, so between the two renames
the profile had no state file: an export in its worker thread packed the plaintext state.json saved before
the key was set, and a session opening a plaintext profile found nothing to read. The previous state is now
kept by a second link for the rollback, so the state file is there throughout. Renames are retried through
a brief Windows sharing violation, a stash that cannot be removed after a save is logged, not raised, and a
failed rollback logs where the previous login is kept. Each check runs at every rename of a real save.
"""

from __future__ import annotations

import json
import logging
import os
import tarfile
from pathlib import Path

import pytest

from app.browser.services.auth_profiles import BrowserAuthProfileService
from tests import test_auth_profile_updates as updates
from tests.test_auth_profile_updates import PROFILE, FakeContext, profile_files, saved, session_saving

# The plain and encrypted profile service, as test_auth_profile_updates gives it: pytest registers a
# fixture under the name it is bound to here.
service = updates.service


def profile_dir(service: BrowserAuthProfileService) -> Path:
    return Path(service.manager.settings.auth_root) / "profiles" / PROFILE


def state_file(service: BrowserAuthProfileService) -> Path:
    (name,) = [name for name in profile_files(service) if name.startswith("state.json")]
    return profile_dir(service) / name


def saved_cookies(service: BrowserAuthProfileService) -> list[str]:
    """The cookie values of the profile's state, decrypted when it is encrypted."""
    state = service.manager.auth_state.prepare_for_context(state_file(service)).storage_state
    loaded = json.loads(Path(state).read_text(encoding="utf-8")) if isinstance(state, str) else state
    return [cookie["value"] for cookie in loaded["cookies"]]


def at_every_rename(monkeypatch, check) -> None:
    """Run ``check(source, destination)`` just before and just after every os.replace."""
    replace = os.replace

    def watched(source, destination) -> None:
        check(source, destination)
        replace(source, destination)
        check(source, destination)

    monkeypatch.setattr(os, "replace", watched)


async def test_the_state_file_is_there_throughout_a_re_save(service, monkeypatch) -> None:
    await saved(service, "OLD-LOGIN")
    state = state_file(service)
    seen: list[bool] = []
    at_every_rename(monkeypatch, lambda _source, _destination: seen.append(state.exists()))

    await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert seen and all(seen), seen


async def test_an_export_during_an_encrypted_re_save_never_packs_the_plaintext_state(
    service, monkeypatch, tmp_path
) -> None:
    """The plaintext state.json saved before the key was set is superseded by state.json.enc only while
    that file is there; an export landing between the renames packed the plaintext copy."""
    if not service.manager.settings.auth_state_encryption_key:
        pytest.skip("an encrypted profile only")
    await saved(service, "OLD-LOGIN")
    (profile_dir(service) / "state.json").write_text('{"cookies": [{"value": "LEGACY-PLAINTEXT"}]}', encoding="utf-8")
    packed: list[list[str]] = []

    def export_now(_source, _destination) -> None:
        archive = tmp_path / f"during-{len(packed)}.tar.gz"
        BrowserAuthProfileService.write_tar(profile_dir(service), archive)
        with tarfile.open(archive) as tar:
            packed.append(sorted(member.name for member in tar.getmembers() if member.isfile()))

    at_every_rename(monkeypatch, export_now)

    await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert packed
    assert not [names for names in packed if f"{PROFILE}/state.json" in names], packed


async def test_a_rename_refused_for_a_moment_is_tried_again(service, monkeypatch) -> None:
    """Windows refuses a rename while another process has the target open (an antivirus scan, a reader)."""
    await saved(service, "OLD-LOGIN")
    state = state_file(service)
    replace = os.replace
    refused: list[str] = []

    def busy_once(source, destination) -> None:
        if Path(destination) == state and not refused:
            refused.append(str(destination))
            raise PermissionError(13, "The process cannot access the file because it is being used", str(destination))
        replace(source, destination)

    monkeypatch.setattr(os, "replace", busy_once)

    await saved(service, "NEW-LOGIN")

    assert refused
    assert saved_cookies(service) == ["NEW-LOGIN"]


async def test_a_previous_state_that_cannot_be_removed_after_a_save_is_logged_not_raised(
    service, monkeypatch, caplog
) -> None:
    await saved(service, "OLD-LOGIN")
    unlink = os.unlink

    def held(path, *args, **kwargs) -> None:
        name = Path(path).name
        if name.startswith(".state.json") and name.endswith(".tmp.json") and Path(path).exists():
            raise PermissionError(13, "The process cannot access the file because it is being used", str(path))
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", held)

    with caplog.at_level(logging.WARNING, logger="app.browser.services.auth_profiles"):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    assert saved_cookies(service) == ["NEW-LOGIN"]
    assert any(".tmp.json" in record.getMessage() for record in caplog.records), caplog.records


async def test_a_failed_rollback_logs_where_the_previous_state_is_kept(service, monkeypatch, caplog) -> None:
    """The metadata move fails, then so does putting the previous state back: the old login must be findable."""
    before = await saved(service, "OLD-LOGIN")
    state = state_file(service)
    replace = os.replace
    onto_state: list[str] = []

    def failing(source, destination) -> None:
        if Path(destination).name == "profile.json":
            raise OSError("metadata rename refused")
        if Path(destination) == state:
            onto_state.append(str(source))
            if len(onto_state) > 1:  # the first is the new state, the second the rollback
                raise OSError("rollback rename refused")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", failing)

    with caplog.at_level(logging.ERROR, logger="app.browser.services.auth_profiles"), pytest.raises(OSError):
        await service.save_for_session(session_saving(FakeContext("NEW-LOGIN")), PROFILE)

    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    kept = [Path(word.strip("'\",.()")) for message in errors for word in message.split() if ".tmp.json" in word]
    assert kept and kept[0].exists(), errors
    assert kept[0].read_bytes() == before[state.name]


async def test_an_encrypted_save_removes_the_plaintext_state_saved_before_the_key(service) -> None:
    """It stayed beside state.json.enc for as long as the profile existed, every cookie in the clear."""
    if not service.manager.settings.auth_state_encryption_key:
        pytest.skip("an encrypted profile only")
    await saved(service, "OLD-LOGIN")
    legacy = profile_dir(service) / "state.json"
    legacy.write_text('{"cookies": [{"value": "LEGACY-PLAINTEXT"}]}', encoding="utf-8")

    await saved(service, "NEW-LOGIN")

    assert not legacy.exists()
    assert saved_cookies(service) == ["NEW-LOGIN"]
