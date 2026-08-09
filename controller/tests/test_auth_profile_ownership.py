"""An auth profile belongs to the operator who saved it.

GHSA-xmh3-cw7j-9gp5: profiles lived in a flat root with no owner, so any caller
who could reach the API could read, export, or open a session against anybody
else's stored logins. Ownership only means something once identity is provable,
which is why it is keyed on `source: "token"` — a profile must not be reachable
by a caller who merely *claims* to be its owner in a header.

Ownership activates with named credentials and not before: a profile saved
without a proven identity records no owner, so shared-token deployments and
profiles that pre-date this release keep working untouched.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.audit import reset_current_operator, set_current_operator
from app.browser.services.auth_profiles import BrowserAuthProfileService


class StubAuthState:
    def inspect(self, path: Path | None) -> dict:
        return {"path": str(path) if path else None, "encrypted": False}

    async def write_storage_state(self, context, destination: Path) -> dict:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}", encoding="utf-8")
        return {"path": str(destination), "encrypted": False}


class StubPage:
    url = "https://example.com/"

    async def title(self) -> str:
        return "Example"


class StubSession:
    id = "session-1"
    context = object()
    page = StubPage()
    last_auth_state_path = None
    auth_profile_name = None


class AuthProfileOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="auto-browser-profile-owner-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        manager = SimpleNamespace(
            settings=SimpleNamespace(auth_root=str(self.root), auth_state_encryption_key=None),
            auth_state=StubAuthState(),
        )
        self.service = BrowserAuthProfileService(manager)
        self._operator_token = None

    def tearDown(self) -> None:
        if self._operator_token is not None:
            reset_current_operator(self._operator_token)

    def as_operator(self, operator_id: str | None, *, source: str = "token") -> None:
        if self._operator_token is not None:
            reset_current_operator(self._operator_token)
            self._operator_token = None
        if operator_id is not None:
            self._operator_token = set_current_operator(operator_id, source=source)

    def save(self, profile_name: str) -> dict:
        return asyncio.run(self.service.save_for_session(StubSession(), profile_name))

    def owner_recorded(self, profile_name: str) -> str | None:
        metadata_path = self.root / "profiles" / profile_name / "profile.json"
        return json.loads(metadata_path.read_text(encoding="utf-8")).get("owner")

    def test_a_proven_identity_is_stamped_as_the_owner(self) -> None:
        self.as_operator("alice")
        self.save("shared-login")
        self.assertEqual(self.owner_recorded("shared-login"), "alice")

    def test_an_unproven_identity_records_no_owner(self) -> None:
        """Shared-token deployments must be unaffected — nothing to lock out."""
        self.as_operator("alice", source="header")
        self.save("shared-login")
        self.assertIsNone(self.owner_recorded("shared-login"))

    def test_another_operator_cannot_read_export_or_open_it(self) -> None:
        self.as_operator("alice")
        self.save("alice-login")

        self.as_operator("bob")
        for action in ("reading", "exporting", "opening a session from"):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                self.service.require_access("alice-login", action=action)

    def test_claiming_to_be_the_owner_in_a_header_is_not_enough(self) -> None:
        """The whole point: the header is a claim, the credential is proof."""
        self.as_operator("alice")
        self.save("alice-login")

        self.as_operator("alice", source="header")
        with self.assertRaises(PermissionError):
            self.service.require_access("alice-login", action="reading")

    def test_the_owner_keeps_access(self) -> None:
        self.as_operator("alice")
        self.save("alice-login")
        self.assertEqual(self.service.require_access("alice-login", action="reading"), "alice")

    def test_ownership_survives_the_owner_saving_again(self) -> None:
        """save_for_session rewrites profile.json wholesale — owner must persist."""
        self.as_operator("alice")
        self.save("alice-login")
        self.save("alice-login")
        self.assertEqual(self.owner_recorded("alice-login"), "alice")

        self.as_operator("bob")
        with self.assertRaises(PermissionError):
            self.save("alice-login")

    def test_an_unowned_profile_stays_accessible_to_everyone(self) -> None:
        """Profiles written before this release have no owner and must not break."""
        self.as_operator(None)
        self.save("legacy")
        self.assertIsNone(self.owner_recorded("legacy"))

        self.as_operator("bob")
        self.assertTrue(self.service.accessible("legacy"))

    def test_listing_hides_profiles_you_cannot_access(self) -> None:
        self.as_operator("alice")
        self.save("alice-login")
        self.as_operator("bob")
        self.save("bob-login")
        self.as_operator(None)
        self.save("legacy")

        self.as_operator("bob")
        listed = {entry["profile_name"] for entry in asyncio.run(self.service.list())}
        self.assertEqual(listed, {"bob-login", "legacy"})


if __name__ == "__main__":
    unittest.main()
