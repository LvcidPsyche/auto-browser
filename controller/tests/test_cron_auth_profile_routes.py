"""The REST cron routes answer an auth profile the caller cannot use, where they answered 500.

POST /crons naming another operator's profile raised ForeignAuthProfileError (a PermissionError the route did not
catch), and POST /crons/{id}/trigger raised FileNotFoundError when the job's profile had gone or had become another
operator's since the job was made (a read answers both as missing). The real auth profile service, cron service and
router run here; the manager's create_session is a stand-in that runs the profile check a session creation runs.
"""

from __future__ import annotations

import errno
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

from app.browser.services.auth_profiles import BrowserAuthProfileService
from app.cron_service import CronService
from app.models import OperatorIdentity
from app.routes.operations import CRON_TRIGGER_AUTH_PROFILE_DETAIL, create_operations_router
from app.session_ownership import acting_as

ALICE = OperatorIdentity(id="alice", source="token")
TOKENS = "alice:" + "a" * 40 + ",bob:" + "b" * 40


class CronAuthProfileRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.auth_root = self.root / "auth"
        settings = SimpleNamespace(auth_root=str(self.auth_root), api_bearer_tokens=TOKENS, api_bearer_token=None)
        self.manager = SimpleNamespace(settings=settings)
        self.profiles = BrowserAuthProfileService(self.manager)
        self.manager.auth_profiles = self.profiles
        self.create_session_error: Exception | None = None

        async def create_session(*, auth_profile: str | None = None, **_kwargs: Any) -> dict[str, Any]:
            if self.create_session_error is not None:
                raise self.create_session_error
            self.profiles.state_path_to_load(auth_profile)  # session_creation.py's own check
            raise AssertionError("the run's session got past the profile check")

        self.manager.create_session = create_session
        self.store_path = self.root / "crons" / "crons.json"
        self.cron = CronService(self.store_path, manager=self.manager, job_queue=object())
        api = FastAPI()
        api.include_router(create_operations_router(manager=self.manager, proxy_store=object(), cron_service=self.cron))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, raise_app_exceptions=False), base_url="http://controller.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    def save_profile(self, name: str, owner: str | None) -> None:
        directory = self.auth_root / "profiles" / name
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {"profile_name": name, **({"owner": owner} if owner else {})}
        (directory / "profile.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / "state.json").write_text("{}", encoding="utf-8")

    def stored_jobs(self) -> dict[str, Any]:
        return json.loads(self.store_path.read_text(encoding="utf-8")) if self.store_path.exists() else {}

    def assert_names_nothing(self, response: httpx.Response, *names: str) -> None:
        for text in ("bob", str(self.root), "profiles", *names):
            self.assertNotIn(text, response.text)

    async def test_creating_a_job_with_another_operators_profile_answers_403_and_creates_nothing(self) -> None:
        self.save_profile("shop-login", owner="bob")
        body = {"name": "nightly", "goal": "check the page", "auth_profile": "shop-login", "schedule": "0 9 * * *"}

        with acting_as(ALICE):
            response = await self.client.post("/crons", json=body)

        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Not permitted"}))
        self.assert_names_nothing(response)
        self.assertEqual(self.stored_jobs(), {})

    async def test_creating_a_job_with_a_profile_not_saved_yet_still_creates_it(self) -> None:
        body = {"name": "nightly", "goal": "check the page", "auth_profile": "not-saved-yet", "schedule": "0 9 * * *"}

        with acting_as(ALICE):
            response = await self.client.post("/crons", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([job["auth_profile"] for job in self.stored_jobs().values()], ["not-saved-yet"])

    async def test_a_store_the_os_will_not_write_is_still_a_server_error_at_create(self) -> None:
        # An OS-level PermissionError carries an errno: the store's fault, which "Not permitted" would hide.
        def denied(_jobs: dict[str, Any]) -> None:
            raise PermissionError(errno.EACCES, "Permission denied", str(self.store_path))

        self.cron._save = denied  # type: ignore[method-assign]

        with acting_as(ALICE):
            response = await self.client.post("/crons", json={"name": "n", "goal": "g", "schedule": "0 9 * * *"})

        self.assertEqual(response.status_code, 500)

    async def _job_with_profile(self, profile: str) -> tuple[str, str]:
        with acting_as(ALICE):
            created = await self.cron.create_job(name="t", goal="g", auth_profile=profile, webhook_enabled=True)
        return created["id"], created["webhook_key"]

    async def test_a_trigger_whose_profile_became_another_operators_answers_409_in_fixed_words(self) -> None:
        self.save_profile("shared", owner=None)
        job_id, key = await self._job_with_profile("shared")
        self.save_profile("shared", owner="bob")  # bob saved it again since: it is his now

        with acting_as(ALICE):
            response = await self.client.post(f"/crons/{job_id}/trigger", json={"webhook_key": key})

        self.assertEqual((response.status_code, response.json()), (409, {"detail": CRON_TRIGGER_AUTH_PROFILE_DETAIL}))
        self.assert_names_nothing(response, "shared")

    async def test_a_trigger_whose_profile_does_not_exist_answers_the_same_409(self) -> None:
        job_id, key = await self._job_with_profile("typo-profile")

        with acting_as(ALICE):
            response = await self.client.post(f"/crons/{job_id}/trigger", json={"webhook_key": key})

        self.assertEqual((response.status_code, response.json()), (409, {"detail": CRON_TRIGGER_AUTH_PROFILE_DETAIL}))
        self.assert_names_nothing(response, "typo-profile")

    async def test_a_trigger_by_another_operator_who_holds_the_key_answers_the_same_409(self) -> None:
        # The trigger checks only the job's key, and the run fires as the job's owner.
        self.save_profile("shared", owner=None)
        job_id, key = await self._job_with_profile("shared")
        self.save_profile("shared", owner="bob")

        with acting_as(OperatorIdentity(id="carol", source="token")):
            response = await self.client.post(f"/crons/{job_id}/trigger", json={"webhook_key": key})

        self.assertEqual((response.status_code, response.json()), (409, {"detail": CRON_TRIGGER_AUTH_PROFILE_DETAIL}))

    async def test_a_file_the_os_cannot_find_is_still_a_server_error_at_the_trigger(self) -> None:
        # One the OS raises carries an errno and a server path: not a state the caller can change.
        job_id, key = await self._job_with_profile("typo-profile")
        self.create_session_error = FileNotFoundError(errno.ENOENT, "No such file or directory", "/usr/bin/chromium")

        with acting_as(ALICE):
            response = await self.client.post(f"/crons/{job_id}/trigger", json={"webhook_key": key})

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("chromium", response.text)
