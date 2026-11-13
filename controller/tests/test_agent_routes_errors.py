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

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_errors import SessionNotFoundError
from app.models import BrowserActionDecision
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.routes.agent import create_agent_router

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


class AgentRouteErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.session = SimpleNamespace(artifact_dir=Path(tmp.name))
        self.manager = SimpleNamespace(
            settings=SimpleNamespace(autonomous_approvals=False, force_governed_approvals=False),
            observe=AsyncMock(return_value={"url": "https://example.com", "title": "t", "text_excerpt": "x"}),
            execute_decision=AsyncMock(return_value={"after": {"url": "https://example.com"}}),
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


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
