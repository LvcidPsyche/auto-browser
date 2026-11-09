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
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit import reset_current_operator, set_current_operator
from app.browser.page_provenance import PageProvenance
from app.browser.services.auth_profiles import BrowserAuthProfileService
from app.main import handle_key_not_found
from app.routes.auth_profiles import create_auth_profiles_router


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
            _page_off_list=lambda session: False,
            _page_refused=lambda page: False,
            _shown_url=lambda page: page.url,
            page_provenance=PageProvenance(),
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

    def test_two_operators_saving_one_new_name_at_once_cannot_both_have_it(self) -> None:
        """ACT-22: a save checked the owner before its slow writes and put its files in place after them, holding only
        its own session's lock, so bob's save of a name alice was saving passed the check (no owner yet) and alice's
        then replaced the profile bob had just been given: each was told its save worked, and bob's login was gone."""

        async def scenario() -> list:
            alice_writing, alice_may_write = asyncio.Event(), asyncio.Event()

            async def write_storage_state(context: str, destination: Path) -> dict:
                if context == "alice":
                    alice_writing.set()
                    await alice_may_write.wait()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps({"cookies": [{"value": context}]}), encoding="utf-8")
                return {"path": str(destination), "encrypted": False}

            self.service.manager.auth_state.write_storage_state = write_storage_state

            async def save_as(operator_id: str) -> dict:
                set_current_operator(operator_id, source="token")  # this task's own context
                session = StubSession()
                session.context = operator_id
                return await self.service.save_for_session(session, "shop")

            alice = asyncio.ensure_future(save_as("alice"))
            await alice_writing.wait()
            bob = asyncio.ensure_future(save_as("bob"))
            await asyncio.wait({bob}, timeout=0.5)  # as far as bob's save gets while alice's is writing
            alice_may_write.set()
            return await asyncio.gather(alice, bob, return_exceptions=True)

        alice_saved, bob_saved = asyncio.run(scenario())

        self.assertEqual(alice_saved["profile_name"], "shop")
        self.assertIsInstance(bob_saved, PermissionError)
        self.assertEqual(self.owner_recorded("shop"), "alice")
        state = json.loads((self.root / "profiles" / "shop" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["cookies"], [{"value": "alice"}])

    def test_saves_of_names_one_folder_holds_wait_for_each_other(self) -> None:
        """Review of c5: the lock was keyed by the name as given, while Windows and macOS find "Shop", "shop" and (on
        Windows) "shop." in one folder, so a save of one passed the owner check while a save of another was writing."""

        async def scenario() -> list[bool]:
            first_writing, first_may_write = asyncio.Event(), asyncio.Event()

            async def write_storage_state(context: str, destination: Path) -> dict:
                if context == "Shop":
                    first_writing.set()
                    await first_may_write.wait()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("{}", encoding="utf-8")
                return {"path": str(destination), "encrypted": False}

            self.service.manager.auth_state.write_storage_state = write_storage_state

            async def save(name: str) -> dict:
                session = StubSession()
                session.context = name
                return await self.service.save_for_session(session, name)

            first = asyncio.ensure_future(save("Shop"))
            await first_writing.wait()
            others = [asyncio.ensure_future(save(name)) for name in ("shop", "SHOP.")]
            await asyncio.wait(others, timeout=0.5)
            finished_while_the_first_wrote = [other.done() for other in others]
            first_may_write.set()
            await asyncio.gather(first, *others, return_exceptions=True)
            return finished_while_the_first_wrote

        self.assertEqual(asyncio.run(scenario()), [False, False])

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

    def test_a_storage_state_path_into_the_profile_is_checked_like_the_profile(self) -> None:
        """`storage_state_path` is relative to AUTH_ROOT, which holds profiles/ too.

        Naming the profile's state file directly used to skip the owner check
        that `auth_profile` gets, so Bob could open a session logged in as Alice.
        """
        self.as_operator("alice")
        self.save("alice-login")

        self.as_operator("bob")
        for path in (
            "profiles/alice-login/state.json",
            "./profiles/../profiles/alice-login/state.json",
            "profiles/alice-login/../alice-login/state.json",
        ):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.service.storage_state_source(path)

        self.as_operator("alice")
        self.assertEqual(
            self.service.storage_state_source("profiles/alice-login/state.json"),
            (self.root / "profiles" / "alice-login" / "state.json").resolve(),
        )

    def test_a_storage_state_path_outside_profiles_needs_no_owner(self) -> None:
        state = self.root / "exports" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}", encoding="utf-8")
        self.as_operator("bob")
        self.assertEqual(self.service.storage_state_source("exports/state.json"), state.resolve())

    def test_damaged_metadata_does_not_read_as_unowned(self) -> None:
        """{} from a torn or corrupt profile.json meant "no owner": open to all."""
        self.as_operator("alice")
        self.save("alice-login")
        (self.root / "profiles" / "alice-login" / "profile.json").write_text('{"owner": "al', encoding="utf-8")

        self.as_operator("bob")
        with self.assertRaises(PermissionError):
            self.service.require_access("alice-login", action="reading")
        with self.assertRaises(PermissionError):
            self.save("alice-login")

    # ── Another operator's profile reads as one that does not exist (AUTH-31) ──

    def get(self, profile_name: str) -> dict:
        return asyncio.run(self.service.get(profile_name))

    def test_another_operators_profile_reads_as_missing(self) -> None:
        self.as_operator("alice")
        self.save("alice-login")

        self.as_operator("bob")
        with self.assertRaises(KeyError) as foreign:
            self.get("alice-login")
        with self.assertRaises(KeyError) as missing:
            self.get("no-such-profile")

        self.assertEqual(foreign.exception.args, ("alice-login",))
        self.assertEqual(missing.exception.args, ("no-such-profile",))
        # The refusal's own text named the owner, and an MCP caller read it.
        self.assertNotIn("alice", str(foreign.exception).replace("alice-login", ""))

    def test_the_owner_still_reads_their_profile(self) -> None:
        self.as_operator("alice")
        self.save("alice-login")

        self.assertEqual(self.get("alice-login")["profile_name"], "alice-login")

    def test_a_profile_answer_names_no_directory(self) -> None:
        self.as_operator("alice")
        self.save("alice-login")

        self.assertNotIn("profile_dir", self.get("alice-login"))
        self.assertNotIn("profile_dir", asyncio.run(self.service.list())[0])

    def test_a_foreign_and_a_missing_profile_answer_rest_callers_byte_for_byte_alike(self) -> None:
        self.as_operator("alice")
        self.save("foreign-login")

        api = FastAPI()
        api.add_exception_handler(KeyError, handle_key_not_found)
        api.include_router(
            create_auth_profiles_router(
                manager=SimpleNamespace(get_auth_profile=self.service.get), settings=SimpleNamespace()
            )
        )
        client = TestClient(api)
        with patch.object(self.service, "verified_operator", return_value="bob"):
            foreign = client.get("/auth-profiles/foreign-login")
            shutil.rmtree(self.root / "profiles" / "foreign-login")
            missing = client.get("/auth-profiles/foreign-login")

        self.assertEqual((foreign.status_code, foreign.content), (missing.status_code, missing.content))
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), {"detail": "Not found: foreign-login"})

    # ── A save that fails after the state is written leaves no state behind (round 4, B6) ──

    def state_files(self, profile_name: str) -> list[str]:
        return sorted(path.name for path in (self.root / "profiles" / profile_name).glob("state.json*"))

    def test_a_title_that_raises_leaves_no_state_file(self) -> None:
        """The cookies were on disk with no metadata, so with no owner: any caller could load them."""

        class RaisingPage(StubPage):
            async def title(self) -> str:
                raise RuntimeError("Target page, context or browser has been closed")

        session = StubSession()
        session.page = RaisingPage()

        with self.assertRaises(RuntimeError):
            asyncio.run(self.service.save_for_session(session, "half-saved"))

        self.assertEqual(self.state_files("half-saved"), [])

    def test_a_metadata_write_that_fails_takes_the_state_file_with_it(self) -> None:
        with patch("app.browser.services.auth_profiles.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save("half-saved")

        self.assertEqual(self.state_files("half-saved"), [])


if __name__ == "__main__":
    unittest.main()
