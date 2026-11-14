"""Another operator's auth profile reads as one that does not exist, on every surface that reads one (AUTH-31).

The refusal told a caller that a profile existed, and over MCP whose it was: the difference between a foreign profile
and a missing one is the oracle, so each read answers exactly what it answers for a name that is not there. A write
(a save, an import that overwrites) cannot read as missing, or it would replace the other's profile or sit beside it;
it stays refused, and its refusal names nobody. The owner's name appears in no answer.
"""

from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.browser.services.auth_profiles import BrowserAuthProfileService
from app.browser_manager import BrowserManager
from app.main import handle_key_not_found
from app.models import McpToolCallRequest
from app.routes.auth_profiles import create_auth_profiles_router
from app.routes.sessions import create_sessions_router
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import _settings

PROFILE = "shop-login"
OWNER = "alice-the-owner"
CALLER = "bob"
STATE_PATHS = ("profiles/shop-login/state.json", "./profiles/../profiles/shop-login/state.json")


class _ForeignProfile(unittest.IsolatedAsyncioTestCase):
    """A real manager and its services, over the REST routes and the MCP gateway, called as an operator who is not
    the profile's owner."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.manager = BrowserManager(_settings(Path(tmp.name)))
        self.service: BrowserAuthProfileService = self.manager.auth_profiles
        self.auth_root = Path(self.manager.settings.auth_root)
        self.profile_dir = self.auth_root / "profiles" / PROFILE
        identity = patch.object(self.service, "verified_operator", return_value=CALLER)
        identity.start()
        self.addCleanup(identity.stop)
        api = FastAPI()
        api.add_exception_handler(KeyError, handle_key_not_found)
        api.include_router(create_auth_profiles_router(manager=self.manager, settings=self.manager.settings))
        api.include_router(create_sessions_router(manager=self.manager))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")
        self.addAsyncCleanup(self.client.aclose)
        self.gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )

    def save_foreign_profile(self, *, owner: str = OWNER) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "profile.json").write_text(
            json.dumps({"profile_name": PROFILE, "owner": owner}), encoding="utf-8"
        )
        (self.profile_dir / "state.json").write_text("{}", encoding="utf-8")

    async def rest_answer(self, method: str, path: str, **kwargs) -> tuple[int, bytes]:
        response = await self.client.request(method, path, **kwargs)
        return response.status_code, response.content

    async def tool_answer(self, tool: str, arguments: dict) -> str:
        response = await self.gateway.call_tool(McpToolCallRequest(name=tool, arguments=arguments))
        self.assertTrue(response.isError, response)
        answer = response.model_dump(exclude_none=True, by_alias=True)
        answer["_meta"].pop("latency_ms", None)  # the one field that differs between any two calls
        return json.dumps(answer, sort_keys=True)

    async def assert_foreign_reads_as_missing(self, answer) -> object:
        """The answer for a foreign profile and for no profile, the same name both times; returns it."""
        self.save_foreign_profile()
        foreign = await answer()
        shutil.rmtree(self.profile_dir)
        missing = await answer()

        self.assertEqual(foreign, missing)
        self.assertNotIn(OWNER, str(foreign))
        return foreign


class ForeignProfileReadsAsMissingTests(_ForeignProfile):
    async def test_rest_get(self) -> None:
        answer = await self.assert_foreign_reads_as_missing(
            lambda: self.rest_answer("GET", f"/auth-profiles/{PROFILE}")
        )

        self.assertEqual(answer, (404, b'{"detail":"Not found: shop-login"}'))

    async def test_rest_export(self) -> None:
        answer = await self.assert_foreign_reads_as_missing(
            lambda: self.rest_answer("GET", f"/auth-profiles/{PROFILE}/export")
        )

        self.assertEqual(answer, (404, b'{"detail":"Not found"}'))

    async def test_rest_create_session_from_a_profile(self) -> None:
        answer = await self.assert_foreign_reads_as_missing(
            lambda: self.rest_answer("POST", "/sessions", json={"auth_profile": PROFILE})
        )

        self.assertEqual(answer, (404, b'{"detail":"Not found"}'))

    async def test_rest_create_session_from_a_storage_state_path_under_profiles(self) -> None:
        for state_path in STATE_PATHS:
            with self.subTest(state_path=state_path):
                answer = await self.assert_foreign_reads_as_missing(
                    lambda: self.rest_answer("POST", "/sessions", json={"storage_state_path": state_path})
                )

                self.assertEqual(answer, (404, b'{"detail":"Not found"}'))

    async def test_mcp_get_auth_profile(self) -> None:
        answer = await self.assert_foreign_reads_as_missing(
            lambda: self.tool_answer("browser.get_auth_profile", {"profile_name": PROFILE})
        )

        self.assertIn(PROFILE, answer)

    async def test_mcp_create_session_from_a_profile(self) -> None:
        answer = await self.assert_foreign_reads_as_missing(
            lambda: self.tool_answer("browser.create_session", {"auth_profile": PROFILE})
        )

        self.assertIn(f"No saved auth profile '{PROFILE}'.", answer)

    async def test_mcp_create_session_from_a_storage_state_path_under_profiles(self) -> None:
        for state_path in STATE_PATHS:
            with self.subTest(state_path=state_path):
                answer = await self.assert_foreign_reads_as_missing(
                    lambda: self.tool_answer("browser.create_session", {"storage_state_path": state_path})
                )

                self.assertIn("in the auth directory", answer)

    async def test_a_name_the_caller_spells_with_spaces_reads_the_same(self) -> None:
        # The missing text repeats the name as the caller wrote it: the foreign one must repeat it the same way.
        padded = f" {PROFILE} "

        async def text_of_the_answer() -> str:
            with self.assertRaises(FileNotFoundError) as opened:
                self.service.state_path_to_load(padded)
            return str(opened.exception)

        answer = await self.assert_foreign_reads_as_missing(text_of_the_answer)

        self.assertIn(repr(padded), answer)

    async def test_a_probe_of_a_storage_state_path_leaves_no_directory_a_later_read_finds(self) -> None:
        # safe_auth_path created the parent of a path it was about to report missing, and export serves any
        # directory: the second probe of a free name then answered unlike a foreign one.
        self.assertEqual(
            (await self.client.post("/sessions", json={"storage_state_path": "profiles/ghost/state.json"})).status_code,
            404,
        )

        self.assertFalse((self.auth_root / "profiles" / "ghost").exists())
        self.assertEqual((await self.client.get("/auth-profiles/ghost/export")).status_code, 404)

    async def test_the_service_raises_each_reads_own_missing_exception(self) -> None:
        self.save_foreign_profile()

        with self.assertRaises(KeyError) as read:
            await self.service.get(PROFILE)
        with self.assertRaises(FileNotFoundError) as exported:
            await self.service.export(PROFILE)
        with self.assertRaises(FileNotFoundError) as opened:
            self.service.storage_state_source("profiles/shop-login/state.json")

        self.assertEqual(read.exception.args, (PROFILE,))
        self.assertEqual(str(exported.exception), f"auth profile '{PROFILE}' not found")
        self.assertEqual(str(opened.exception), "No file 'profiles/shop-login/state.json' in the auth directory")

    async def test_the_owner_still_reads_every_surface(self) -> None:
        self.save_foreign_profile(owner=CALLER)

        got = await self.client.get(f"/auth-profiles/{PROFILE}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["profile_name"], PROFILE)
        exported = await self.client.get(f"/auth-profiles/{PROFILE}/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(
            self.service.storage_state_source("profiles/shop-login/state.json"),
            (self.profile_dir / "state.json").resolve(),
        )


class AWriteStaysRefusedTests(_ForeignProfile):
    """A write cannot read as missing: it would silently replace the other's profile, or sit beside it."""

    def expected_refusal(self, action: str) -> str:
        return (
            f"auth profile '{PROFILE}' belongs to another operator; {action} requires authenticating as that operator"
        )

    async def test_a_save_over_a_foreign_profile_is_refused_and_names_nobody(self) -> None:
        self.save_foreign_profile()
        session = SimpleNamespace(id="s1", context=object(), page=SimpleNamespace(url="https://example.com/"))

        with self.assertRaises(PermissionError) as refused:
            await self.service.save_for_session(session, PROFILE)  # type: ignore[arg-type]

        self.assertEqual(str(refused.exception), self.expected_refusal("saving an auth profile"))
        self.assertEqual(json.loads((self.profile_dir / "profile.json").read_text())["owner"], OWNER)

    async def test_an_import_that_overwrites_a_foreign_profile_is_refused_and_names_nobody(self) -> None:
        self.save_foreign_profile()
        archive = self.auth_root / "incoming.tar.gz"
        source = self.auth_root / "incoming"
        (source / PROFILE).mkdir(parents=True)
        (source / PROFILE / "state.json").write_text("{}", encoding="utf-8")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source / PROFILE, arcname=PROFILE)

        with self.assertRaises(PermissionError) as refused:
            await self.service.import_profile(archive.name, overwrite=True)

        self.assertEqual(str(refused.exception), self.expected_refusal("overwriting an auth profile"))
        over_rest = await self.client.post(
            "/auth-profiles/import", json={"archive_path": archive.name, "overwrite": True}
        )
        self.assertEqual(over_rest.status_code, 403, over_rest.text)
        self.assertNotIn(OWNER, over_rest.text)

    async def test_require_access_names_nobody_for_any_caller(self) -> None:
        self.save_foreign_profile()

        with self.assertRaises(PermissionError) as refused:
            self.service.require_access(PROFILE, action="scheduling a cron job with it")

        self.assertEqual(str(refused.exception), self.expected_refusal("scheduling a cron job with it"))


class ADamagedProfileIsNotAForeignOneTests(_ForeignProfile):
    """A profile.json that cannot be read has no owner to name: it is not known to be another's, so it keeps its own
    refusal, which says what to fix, on every read."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.save_foreign_profile()
        (self.profile_dir / "profile.json").write_text('{"owner": "al', encoding="utf-8")

    async def test_rest_get_keeps_its_refusal(self) -> None:
        response = await self.client.get(f"/auth-profiles/{PROFILE}")

        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Not permitted"}))

    async def test_the_reads_are_not_answered_as_missing(self) -> None:
        for read in (self.service.get, self.service.export):
            with self.assertRaises(PermissionError) as refused:
                await read(PROFILE)
            self.assertIn("restore or delete its profile.json", str(refused.exception))
        with self.assertRaises(PermissionError):
            self.service.storage_state_source("profiles/shop-login/state.json")


class TheProfileAnswerNamesNoServerPathTests(_ForeignProfile):
    async def test_get_and_list_answer_no_path(self) -> None:
        self.save_foreign_profile(owner=CALLER)

        got = await self.service.get(PROFILE)
        listed = await self.service.list()

        self.assertEqual(list(got), ["profile_name", "auth_state", "metadata"])
        self.assertNotIn("path", got["auth_state"])
        self.assertNotIn("path", listed[0]["auth_state"])
        self.assertTrue(got["auth_state"]["exists"])

    async def test_a_sessions_own_auth_state_keeps_its_path(self) -> None:
        session = SimpleNamespace(
            last_auth_state_path=self.auth_root / "state.json", auth_dir=self.auth_root, auth_profile_name=None
        )

        self.assertIn("path", self.service.session_auth_state_info(session))  # type: ignore[arg-type]


class TheListSkipsWhatIsNoProfileTests(_ForeignProfile):
    """A directory under profiles/ whose name is no profile name (a file manager's .Trash-1000) made the whole list
    raise ValueError: GET /auth-profiles answered 500 and the tool an error, for every profile beside it."""

    def leave_strays(self) -> None:
        root = self.auth_root / "profiles"
        for name in (".Trash-1000", "lost+found", "a name with spaces", "x" * 121):
            (root / name).mkdir(parents=True)
        (root / ".Trash-1000" / "state.json").write_text("{}", encoding="utf-8")
        (root / "notes.txt").write_text("not a profile", encoding="utf-8")
        (root / "stray-file").write_text("a file with a profile's kind of name", encoding="utf-8")

    async def test_the_rest_list_answers_the_profile_beside_a_stray_directory_and_a_stray_file(self) -> None:
        self.save_foreign_profile(owner=CALLER)
        self.leave_strays()

        response = await self.client.get("/auth-profiles")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([entry["profile_name"] for entry in response.json()], [PROFILE])

    async def test_the_tool_lists_the_profile_too(self) -> None:
        self.save_foreign_profile(owner=CALLER)
        self.leave_strays()

        response = await self.gateway.call_tool(McpToolCallRequest(name="browser.list_auth_profiles", arguments={}))

        self.assertFalse(response.isError, response)
        self.assertEqual([entry["profile_name"] for entry in response.structuredContent], [PROFILE])

    async def test_only_strays_is_an_empty_list(self) -> None:
        self.leave_strays()

        self.assertEqual(await self.service.list(), [])


class ToolDescriptionTests(unittest.TestCase):
    def test_the_tool_promises_no_directory(self) -> None:
        spec = next(
            tool
            for tool in McpToolGateway(
                manager=SimpleNamespace(),
                orchestrator=SimpleNamespace(),
                job_queue=SimpleNamespace(),
                tool_profile="full",
            ).list_tools()
            if tool["name"] == "browser.get_auth_profile"
        )

        self.assertNotIn("director", spec["description"])
        self.assertIn("storage-state metadata", spec["description"])


if __name__ == "__main__":
    unittest.main()
