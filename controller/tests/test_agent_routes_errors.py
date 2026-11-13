"""What POST /sessions/{id}/agent/step and /agent/run answer for the errors a step can end in.

A RuntimeError that left the orchestrator was answered 503 and written nowhere, whatever it was (A5). A step given an
approval id no record answers to was a failed provider step, 502 with an ERROR and its traceback, where the action
routes answer it 404 and write nothing (A10).
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_errors import SessionNotFoundError
from app.models import BrowserActionDecision
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.routes.agent import create_agent_router
from tests.test_approval_spend import SESSION, START_URL, SpendBase

STEP = "/sessions/s1/agent/step"
RUN = "/sessions/s1/agent/run"
BODY = {"provider": "openai", "goal": "inspect the page"}
SECRET_PATH = "/data/artifacts/s1/secret.jsonl"


class _Adapter:
    default_model = "test-model"

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure

    async def decide(self, **_kwargs):
        if self.failure is not None:
            raise self.failure
        return ProviderDecision(
            provider="openai",
            model="test-model",
            decision=BrowserActionDecision(action="click", reason="go", element_id="op-a", risk_category="write"),
        )


class _Registry:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def get(self, _name):
        return self.adapter


class _AgentRouteCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.session = SimpleNamespace(artifact_dir=Path(tmp.name))
        self.manager = SimpleNamespace(
            settings=SimpleNamespace(autonomous_approvals=False, force_governed_approvals=False),
            observe=AsyncMock(return_value={"url": "https://example.com", "title": "t", "text_excerpt": "x"}),
            execute_decision=AsyncMock(return_value={"after": {"url": "https://example.com"}}),
            get_approval=AsyncMock(return_value={"id": "a-known-one", "status": "approved"}),
            require_governed_approval=AsyncMock(return_value=None),
            get_session=AsyncMock(return_value=self.session),
            get_session_summary=AsyncMock(return_value={"id": "s1"}),
            _append_jsonl=AsyncMock(),
        )

    def client(self, adapter: _Adapter | None = None, *, orchestrator=None) -> TestClient:
        orchestrator = orchestrator or BrowserOrchestrator(self.manager, _Registry(adapter or _Adapter()))
        app = FastAPI()
        app.include_router(create_agent_router(manager=self.manager, orchestrator=orchestrator, job_queue=None))
        return TestClient(app)

    def _stand_in_that_raises(self, failure: BaseException) -> SimpleNamespace:
        return SimpleNamespace(step=AsyncMock(side_effect=failure), run=AsyncMock(side_effect=failure))


class AgentRouteErrorTests(_AgentRouteCase):
    # A5: a RuntimeError that left the orchestrator.

    def test_a_runtime_error_that_is_no_providers_refusal_is_logged_with_its_cause_and_answered_503(self) -> None:
        for route in (STEP, RUN):
            with self.subTest(route=route), self.assertLogs("app.routes.agent", level="WARNING") as logged:
                client = self.client(
                    orchestrator=self._stand_in_that_raises(RuntimeError(f"cannot write {SECRET_PATH}"))
                )

                response = client.post(route, json=BODY)

                self.assertEqual((response.status_code, response.json()), (503, {"detail": "Service unavailable"}))
                (record,) = logged.records
                written = record.getMessage()
                self.assertIn("answered 503 for RuntimeError:", written)
                self.assertIn(SECRET_PATH, written)  # the cause is in the log, and only there
                self.assertIn("session 's1'", written)
                self.assertIsNone(record.exc_info)

    def test_a_failure_inside_the_step_says_agent_step_failed_and_its_cause_is_in_the_log(self) -> None:
        # The provider's call raised a RuntimeError that is no refusal of the provider's (an unexplained fault): the
        # step ends as an error result, answered 502 with the fixed text, and the orchestrator logs why.
        client = self.client(_Adapter(RuntimeError(f"cannot use {SECRET_PATH}")))

        with self.assertLogs("app.orchestrator", level="ERROR") as logged:
            response = client.post(STEP, json=BODY)

        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertEqual(
            (detail["status"], detail["error"], detail["error_code"]), ("error", "Agent step failed", None)
        )
        self.assertNotIn(SECRET_PATH, response.text)
        self.assertIn("RuntimeError", logged.records[0].getMessage())
        self.assertIn(SECRET_PATH, logged.records[0].getMessage())

    def test_a_session_that_is_not_known_still_answers_unknown_session_and_writes_nothing(self) -> None:
        self.manager.get_session.side_effect = SessionNotFoundError("s1")
        for route in (STEP, RUN):
            with self.subTest(route=route), self.assertNoLogs("app", level="WARNING"):
                response = self.client().post(route, json=BODY)

                self.assertEqual((response.status_code, response.json()), (404, {"detail": "Unknown session"}))


UNKNOWN_APPROVAL = "ffffffffffff"
WITH_APPROVAL = {**BODY, "approval_id": UNKNOWN_APPROVAL}


class UnknownApprovalIdTests(_AgentRouteCase):
    """A10: an approval id no record answers to, or another operator's (the store answers both with the same
    KeyError), is answered as the action routes answer it."""

    def _the_store_does_not_know_it(self) -> None:
        self.manager.get_approval.side_effect = KeyError(UNKNOWN_APPROVAL)

    def test_it_is_a_404_with_the_action_routes_text_and_nothing_is_logged(self) -> None:
        self._the_store_does_not_know_it()
        for route in (STEP, RUN):
            with self.subTest(route=route), self.assertNoLogs("app", level="DEBUG"):
                response = self.client().post(route, json=WITH_APPROVAL)

                self.assertEqual(
                    (response.status_code, response.json()), (404, {"detail": f"Not found: {UNKNOWN_APPROVAL}"})
                )

    def test_another_operators_id_and_an_unknown_one_read_alike(self) -> None:
        answers = []
        for approval_id in ("a-record-of-another-operators", "a-record-nobody-made"):
            self.manager.get_approval.side_effect = KeyError(approval_id)
            response = self.client().post(STEP, json={**BODY, "approval_id": approval_id})
            answers.append((response.status_code, response.text.replace(approval_id, "<ID>")))

        self.assertEqual(answers[0], answers[1])

    def test_a_session_that_is_not_known_still_answers_unknown_session_with_an_approval_id_given(self) -> None:
        self.manager.get_session.side_effect = SessionNotFoundError("s1")
        for route in (STEP, RUN):
            with self.subTest(route=route):
                response = self.client().post(route, json=WITH_APPROVAL)

                self.assertEqual((response.status_code, response.json()), (404, {"detail": "Unknown session"}))

    def test_a_key_error_that_names_something_else_is_a_failed_step_as_before(self) -> None:
        # Only the approval id's own KeyError is the caller's miss: a bug's is still an error result and a line in the log.
        self.manager.execute_decision.side_effect = KeyError("not-the-approval-id")

        with self.assertLogs("app.orchestrator", level="ERROR"):
            response = self.client().post(STEP, json=WITH_APPROVAL)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["error"], "Agent step failed")

    def test_the_governed_profile_answers_it_alike(self) -> None:
        self._the_store_does_not_know_it()

        with self.assertNoLogs("app", level="DEBUG"):
            response = self.client().post(STEP, json={**WITH_APPROVAL, "workflow_profile": "governed"})

        self.assertEqual((response.status_code, response.json()), (404, {"detail": f"Not found: {UNKNOWN_APPROVAL}"}))

    def test_a_step_with_no_approval_id_is_unchanged(self) -> None:
        self.manager.execute_decision.side_effect = KeyError("whatever")

        with self.assertLogs("app.orchestrator", level="ERROR"):
            response = self.client().post(STEP, json=BODY)

        self.assertEqual(response.status_code, 502)
        self.manager.get_approval.assert_not_awaited()  # there is no id to look up

    def test_a_known_id_is_looked_up_once_and_the_step_goes_on(self) -> None:
        for route in (STEP, RUN):
            with self.subTest(route=route):
                self.manager.get_approval.reset_mock()

                response = self.client().post(route, json=WITH_APPROVAL)

                self.assertEqual(response.status_code, 200)
                self.manager.get_approval.assert_awaited_once_with(UNKNOWN_APPROVAL)


class UnknownApprovalIdRealStoreTests(SpendBase):
    """The same, with the controller's own approval store raising the KeyError."""

    async def _post_step(self, approval_id: str) -> httpx.Response:
        self.manager.observe = AsyncMock(  # type: ignore[method-assign]
            return_value={"url": START_URL, "title": "t", "text_excerpt": "x"}
        )
        adapter = _Adapter()
        adapter.decide = AsyncMock(  # type: ignore[method-assign]
            return_value=ProviderDecision(
                provider="openai",
                model="test-model",
                decision=BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment"),
            )
        )
        app = FastAPI()
        app.include_router(
            create_agent_router(
                manager=self.manager,
                orchestrator=BrowserOrchestrator(self.manager, _Registry(adapter)),
                job_queue=None,
            )
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/sessions/{SESSION}/agent/step", json={**BODY, "approval_id": approval_id})

    async def test_an_id_the_store_does_not_hold_is_a_404_and_writes_nothing(self) -> None:
        with self.assertNoLogs("app", level="WARNING"):
            response = await self._post_step(UNKNOWN_APPROVAL)

        self.assertEqual((response.status_code, response.json()), (404, {"detail": f"Not found: {UNKNOWN_APPROVAL}"}))


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
