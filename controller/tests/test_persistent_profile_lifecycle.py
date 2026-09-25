"""Finding 3: auth-profile delete/rename/import must move the on-disk
persistent browser profile together with the encrypted export, through
browser-node's authenticated API, after the ownership check, and never while a
live session holds it."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.audit import reset_current_operator, set_current_operator
from app.browser.services.auth_profiles import ProfileInUseError
from app.browser_manager import BrowserManager
from app.config import Settings
from app.persistent_profiles import PersistentProfileError


def _settings(root: Path, **overrides) -> Settings:
    kwargs = dict(
        _env_file=None,
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
        BROWSER_WS_ENDPOINT_FILE=str(root / "missing-ws.txt"),
        WITNESS_ENABLED=False,
        PERSISTENT_PROFILES_ENABLED=True,
        PROFILE_CONTROL_TOKEN="test-token",
        SESSION_ISOLATION_MODE="shared_browser_node",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class _Base(unittest.IsolatedAsyncioTestCase):
    settings_overrides: dict = {}

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root, **self.settings_overrides))
        self.manager.audit.append = AsyncMock()
        self.profiles = self.manager.auth_profiles
        self.profiles._record_profile_receipt = AsyncMock()
        self.calls: list[tuple] = []
        self.trash = AsyncMock(side_effect=self._trash)
        self.rename = AsyncMock(side_effect=self._rename)
        self.manager.persistent_profiles.trash = self.trash
        self.manager.persistent_profiles.rename = self.rename

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _trash(self, name, *, reason, owner):
        # Records whether the export still existed at the moment of the call,
        # to prove ordering (browser profile first, export second).
        self.calls.append(("trash", name, reason, owner, (self.export_dir(name)).exists()))
        return {"trashed": True, "existed": True}

    async def _rename(self, name, new_name, *, owner):
        self.calls.append(("rename", name, new_name, owner, self.export_dir(name).exists()))
        return {"renamed": True, "existed": True}

    def export_dir(self, name: str) -> Path:
        return self.root / "auth" / "profiles" / name

    def write_profile(self, name: str, *, owner: str | None = None) -> None:
        directory = self.export_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        payload = {"profile_name": name}
        if owner:
            payload["owner"] = owner
        (directory / "profile.json").write_text(json.dumps(payload), encoding="utf-8")

    def hold_profile(self, name: str) -> None:
        session = unittest.mock.Mock()
        session.id = "live-1"
        session.persistent_profile_name = name
        session.persistent_profile_released = False
        self.manager.sessions["live-1"] = session

    def write_archive(self, top_level: str) -> str:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            data = b'{"cookies": [], "origins": []}'
            info = tarfile.TarInfo(f"{top_level}/state.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        name = f"{top_level}-import.tar.gz"
        (self.root / "auth").mkdir(parents=True, exist_ok=True)
        (self.root / "auth" / name).write_bytes(buffer.getvalue())
        return name


class DeleteTests(_Base):
    async def test_delete_trashes_the_browser_profile_before_removing_the_export(self) -> None:
        self.write_profile("nihad-google")
        result = await self.profiles.delete("nihad-google")
        self.assertEqual(self.calls, [("trash", "nihad-google", "deleted", None, True)])
        self.assertFalse(self.export_dir("nihad-google").exists())
        self.assertTrue(result["browser_profile_trashed"])

    async def test_delete_passes_the_verified_owner(self) -> None:
        self.write_profile("alice-login", owner="alice")
        token = set_current_operator("alice", source="token")
        try:
            await self.profiles.delete("alice-login")
        finally:
            reset_current_operator(token)
        self.assertEqual(self.calls[0][3], "alice")

    async def test_delete_by_someone_else_touches_nothing(self) -> None:
        self.write_profile("alice-login", owner="alice")
        token = set_current_operator("mallory", source="token")
        try:
            with self.assertRaises(PermissionError):
                await self.profiles.delete("alice-login")
        finally:
            reset_current_operator(token)
        self.trash.assert_not_awaited()
        self.assertTrue(self.export_dir("alice-login").exists())

    async def test_trash_failure_keeps_the_export(self) -> None:
        self.write_profile("nihad-google")
        self.manager.persistent_profiles.trash = AsyncMock(side_effect=PersistentProfileError("down", status_code=502))
        with self.assertRaises(PersistentProfileError):
            await self.profiles.delete("nihad-google")
        self.assertTrue(self.export_dir("nihad-google").exists())

    async def test_browser_side_owner_mismatch_becomes_permission_error(self) -> None:
        self.write_profile("nihad-google")
        self.manager.persistent_profiles.trash = AsyncMock(side_effect=PersistentProfileError("owner", status_code=403))
        with self.assertRaises(PermissionError):
            await self.profiles.delete("nihad-google")
        self.assertTrue(self.export_dir("nihad-google").exists())

    async def test_delete_refused_while_a_live_session_holds_the_profile(self) -> None:
        self.write_profile("nihad-google")
        self.hold_profile("nihad-google")
        with self.assertRaises(ProfileInUseError):
            await self.profiles.delete("nihad-google")
        self.trash.assert_not_awaited()
        self.assertTrue(self.export_dir("nihad-google").exists())

    async def test_browser_profile_without_an_export_can_still_be_deleted(self) -> None:
        # e.g. owner-default opened and logged into, but never saved yet
        result = await self.profiles.delete("owner-default")
        self.assertTrue(result["deleted"])
        self.assertEqual(self.calls[0][:3], ("trash", "owner-default", "deleted"))

    async def test_nothing_anywhere_is_not_found(self) -> None:
        self.manager.persistent_profiles.trash = AsyncMock(return_value={"trashed": False, "existed": False})
        with self.assertRaises(FileNotFoundError):
            await self.profiles.delete("ghost")


class RenameTests(_Base):
    async def test_rename_moves_the_browser_profile_first(self) -> None:
        self.write_profile("old-name")
        await self.profiles.rename("old-name", "new-name")
        self.assertEqual(self.calls, [("rename", "old-name", "new-name", None, True)])
        self.assertTrue(self.export_dir("new-name").exists())
        self.assertFalse(self.export_dir("old-name").exists())

    async def test_rename_failure_changes_nothing(self) -> None:
        self.write_profile("old-name")
        self.manager.persistent_profiles.rename = AsyncMock(side_effect=PersistentProfileError("down", status_code=502))
        with self.assertRaises(PersistentProfileError):
            await self.profiles.rename("old-name", "new-name")
        self.assertTrue(self.export_dir("old-name").exists())
        self.assertFalse(self.export_dir("new-name").exists())

    async def test_rename_refused_while_either_name_is_live(self) -> None:
        self.write_profile("old-name")
        self.hold_profile("new-name")
        with self.assertRaises(ProfileInUseError):
            await self.profiles.rename("old-name", "new-name")
        self.rename.assert_not_awaited()

    async def test_rename_by_someone_else_touches_nothing(self) -> None:
        self.write_profile("alice-login", owner="alice")
        with self.assertRaises(PermissionError):
            await self.profiles.rename("alice-login", "mine-now")
        self.rename.assert_not_awaited()


class ImportTests(_Base):
    async def test_import_over_an_existing_name_resets_its_browser_profile(self) -> None:
        self.write_profile("nihad-google")
        archive = self.write_archive("nihad-google")
        await self.profiles.import_profile(archive, overwrite=True)
        self.assertEqual(self.calls[0][:4], ("trash", "nihad-google", "replaced-by-import", None))

    async def test_import_of_a_new_name_still_retires_any_stale_browser_profile(self) -> None:
        archive = self.write_archive("brand-new")
        await self.profiles.import_profile(archive)
        self.assertEqual(self.calls[0][:3], ("trash", "brand-new", "replaced-by-import"))
        self.assertTrue(self.export_dir("brand-new").exists())

    async def test_import_without_overwrite_onto_an_existing_name_touches_nothing(self) -> None:
        self.write_profile("nihad-google")
        archive = self.write_archive("nihad-google")
        with self.assertRaises(FileExistsError):
            await self.profiles.import_profile(archive)
        self.trash.assert_not_awaited()

    async def test_import_refused_while_the_profile_is_live(self) -> None:
        self.write_profile("nihad-google")
        self.hold_profile("nihad-google")
        archive = self.write_archive("nihad-google")
        with self.assertRaises(ProfileInUseError):
            await self.profiles.import_profile(archive, overwrite=True)
        self.trash.assert_not_awaited()

    async def test_overwrite_of_someone_elses_profile_touches_nothing(self) -> None:
        self.write_profile("alice-login", owner="alice")
        archive = self.write_archive("alice-login")
        with self.assertRaises(PermissionError):
            await self.profiles.import_profile(archive, overwrite=True)
        self.trash.assert_not_awaited()


class ImportAtomicityTests(_Base):
    """Re-review finding 2: nothing is trashed until the archive is fully
    extracted; a failure after that puts everything back."""

    def write_bad_archive(self, top_level: str) -> str:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            good = b'{"cookies": [], "origins": []}'
            info = tarfile.TarInfo(f"{top_level}/state.json")
            info.size = len(good)
            tar.addfile(info, io.BytesIO(good))
            info = tarfile.TarInfo(f"{top_level}/../escape.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        name = f"{top_level}-bad.tar.gz"
        (self.root / "auth").mkdir(parents=True, exist_ok=True)
        (self.root / "auth" / name).write_bytes(buffer.getvalue())
        return name

    async def test_a_bad_archive_never_touches_the_browser_profile(self) -> None:
        self.write_profile("nihad-google")
        archive = self.write_bad_archive("nihad-google")
        with self.assertRaises(Exception):
            await self.profiles.import_profile(archive, overwrite=True)
        self.trash.assert_not_awaited()
        self.assertTrue((self.export_dir("nihad-google") / "state.json").exists())

    async def test_an_oversized_member_never_touches_the_browser_profile(self) -> None:
        from app.browser.services import auth_profiles as module

        self.write_profile("nihad-google")
        archive = self.write_archive("nihad-google")
        original = module.MAX_ARCHIVE_MEMBER_BYTES
        module.MAX_ARCHIVE_MEMBER_BYTES = 4
        try:
            with self.assertRaises(ValueError):
                await self.profiles.import_profile(archive, overwrite=True)
        finally:
            module.MAX_ARCHIVE_MEMBER_BYTES = original
        self.trash.assert_not_awaited()

    async def test_trash_happens_after_the_new_export_is_in_place(self) -> None:
        self.write_profile("nihad-google")
        (self.export_dir("nihad-google") / "old-marker").write_text("old", encoding="utf-8")
        archive = self.write_archive("nihad-google")
        seen: list[bool] = []

        async def trash(name, *, reason, owner):
            seen.append((self.export_dir(name) / "old-marker").exists())
            return {"trashed": True}

        self.manager.persistent_profiles.trash = AsyncMock(side_effect=trash)
        await self.profiles.import_profile(archive, overwrite=True)
        self.assertEqual(seen, [False])  # new export already swapped in
        self.assertFalse((self.export_dir("nihad-google") / "old-marker").exists())

    async def test_trash_failure_restores_the_previous_export(self) -> None:
        self.write_profile("nihad-google")
        (self.export_dir("nihad-google") / "old-marker").write_text("old", encoding="utf-8")
        archive = self.write_archive("nihad-google")
        self.manager.persistent_profiles.trash = AsyncMock(side_effect=PersistentProfileError("down", status_code=502))
        with self.assertRaises(PersistentProfileError):
            await self.profiles.import_profile(archive, overwrite=True)
        self.assertTrue((self.export_dir("nihad-google") / "old-marker").exists())
        leftovers = [p.name for p in (self.root / "auth").iterdir() if p.name.startswith(".import-")]
        self.assertEqual(leftovers, [])

    async def test_trash_failure_on_a_new_name_leaves_no_export_behind(self) -> None:
        archive = self.write_archive("brand-new")
        self.manager.persistent_profiles.trash = AsyncMock(side_effect=PersistentProfileError("down", status_code=502))
        with self.assertRaises(PersistentProfileError):
            await self.profiles.import_profile(archive)
        self.assertFalse(self.export_dir("brand-new").exists())


class LeaseLockTests(_Base):
    """Delete/rename/import hold the same per-profile lock as Open."""

    async def test_delete_waits_for_an_open_in_progress(self) -> None:
        import asyncio

        self.write_profile("nihad-google")
        lock = self.manager.session_lifecycle.profile_lease_lock("nihad-google")
        await lock.acquire()
        task = asyncio.create_task(self.profiles.delete("nihad-google"))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.trash.assert_not_awaited()
        lock.release()
        await task
        self.trash.assert_awaited_once()


class FeatureOffTests(_Base):
    settings_overrides = {"PERSISTENT_PROFILES_ENABLED": False}

    async def test_nothing_calls_browser_node_when_persistent_profiles_are_off(self) -> None:
        self.write_profile("a")
        await self.profiles.rename("a", "b")
        await self.profiles.delete("b")
        archive = self.write_archive("c")
        await self.profiles.import_profile(archive)
        self.trash.assert_not_awaited()
        self.rename.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
