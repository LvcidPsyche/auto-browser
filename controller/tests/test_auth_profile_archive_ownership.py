"""An exported archive can be imported only by the operator who exported it.

Under named credentials (API_BEARER_TOKENS) an archive sits in AUTH_ROOT where
any operator's import_profile can reach it. Export now records who made it, and
import refuses anyone else. The importer never becomes the owner of what they
import: the owner recorded inside the archive stays, so a refusal here cannot be
turned into a takeover by importing instead.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.audit import reset_current_operator, set_current_operator
from app.browser.services.auth_profiles import EXPORTER_SIDECAR_SUFFIX, BrowserAuthProfileService


class _Audit:
    async def append(self, **_kwargs) -> None:
        return None


class ArchiveOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.service = BrowserAuthProfileService(
            SimpleNamespace(
                settings=SimpleNamespace(auth_root=str(self.root), auth_state_encryption_key=None),
                audit=_Audit(),
                witness=None,
            )
        )
        self._operator_token = None

    async def asyncTearDown(self) -> None:
        self.as_operator(None)
        self.tmp.cleanup()

    def as_operator(self, operator_id: str | None, *, source: str = "token") -> None:
        if self._operator_token is not None:
            reset_current_operator(self._operator_token)
            self._operator_token = None
        if operator_id is not None:
            self._operator_token = set_current_operator(operator_id, source=source)

    def _profile(self, name: str, owner: str | None) -> Path:
        directory = self.root / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "state.json").write_text('{"cookies": []}', encoding="utf-8")
        (directory / "profile.json").write_text(json.dumps({"owner": owner} if owner else {}), encoding="utf-8")
        return directory

    def _remove_profile(self, name: str) -> None:
        directory = self.root / "profiles" / name
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()

    def _owner_on_disk(self, name: str) -> str | None:
        return json.loads((self.root / "profiles" / name / "profile.json").read_text(encoding="utf-8")).get("owner")

    async def _alice_exports(self, name: str = "alice-login") -> str:
        self._profile(name, "alice")
        self.as_operator("alice")
        exported = await self.service.export(name)
        self._remove_profile(name)
        return exported["archive_name"]

    async def test_another_operator_cannot_import_the_archive(self) -> None:
        archive = await self._alice_exports()

        self.as_operator("bob")
        with self.assertRaises(PermissionError):
            await self.service.import_profile(archive)
        self.assertFalse((self.root / "profiles" / "alice-login").exists(), "nothing may be extracted")

    async def test_the_exporter_can_import_it_and_the_recorded_owner_stays(self) -> None:
        archive = await self._alice_exports()

        self.as_operator("alice")
        result = await self.service.import_profile(archive)

        self.assertTrue(result["imported"])
        self.assertEqual(self._owner_on_disk("alice-login"), "alice")

    async def test_the_importer_never_becomes_the_owner(self) -> None:
        """An archive nobody recorded an exporter for: the owner inside it still decides."""
        archive = await self._alice_exports()
        (self.root / f"{archive}{EXPORTER_SIDECAR_SUFFIX}").unlink(missing_ok=True)

        self.as_operator("bob")
        await self.service.import_profile(archive)

        self.assertEqual(self._owner_on_disk("alice-login"), "alice")
        with self.assertRaises(PermissionError):
            self.service.require_access("alice-login", action="reading")

    async def test_claiming_to_be_the_exporter_in_a_header_is_not_enough(self) -> None:
        archive = await self._alice_exports()

        self.as_operator("alice", source="header")
        with self.assertRaises(PermissionError):
            await self.service.import_profile(archive)

    async def test_an_unreadable_binding_does_not_read_as_unbound(self) -> None:
        archive = await self._alice_exports()
        (self.root / f"{archive}{EXPORTER_SIDECAR_SUFFIX}").write_text('{"exporter": "al', encoding="utf-8")

        self.as_operator("bob")
        with self.assertRaises(PermissionError):
            await self.service.import_profile(archive)

    async def test_without_named_credentials_nothing_is_bound(self) -> None:
        """Shared-token and no-token deployments export and import as before."""
        self._profile("shared-login", None)
        self.as_operator("someone", source="header")
        archive = (await self.service.export("shared-login"))["archive_name"]
        self.assertEqual(list(self.root.glob(f"*{EXPORTER_SIDECAR_SUFFIX}")), [])
        self._remove_profile("shared-login")

        self.as_operator(None)
        self.assertTrue((await self.service.import_profile(archive))["imported"])

    async def test_an_archive_bound_to_an_operator_is_not_importable_without_credentials(self) -> None:
        archive = await self._alice_exports()

        self.as_operator(None)
        with self.assertRaises(PermissionError):
            await self.service.import_profile(archive)

    async def test_export_does_not_advertise_a_download_url_nothing_serves(self) -> None:
        self._profile("alice-login", "alice")
        self.as_operator("alice")

        exported = await self.service.export("alice-login")

        self.assertNotIn("download_url", exported)


if __name__ == "__main__":
    unittest.main()
