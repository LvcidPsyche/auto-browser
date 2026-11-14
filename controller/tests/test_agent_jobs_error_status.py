"""A job whose run or step reports an error is recorded failed, not completed."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.agent_jobs import AgentJobQueue
from app.models import AgentRunRequest, AgentRunResult, AgentStepRequest, AgentStepResult
from tests.test_agent_jobs import every_session_usable

ERROR = "provider returned garbage"


def _step(kwargs, status, error=None, error_code=None) -> AgentStepResult:
    return AgentStepResult(
        provider=kwargs["provider_name"],
        model="m",
        goal=kwargs["goal"],
        workflow_profile="fast",
        status=status,
        observation={"url": "https://example.com"},
        decision={},
        error=error,
        error_code=error_code,
    )


class _Orchestrator:
    def __init__(self, status: str) -> None:
        self.status = status

    def _error(self) -> str | None:
        return ERROR if self.status == "error" else None

    async def step(self, **kwargs):
        return _step(kwargs, self.status, self._error(), 502 if self.status == "error" else None)

    async def run(self, **kwargs):
        return AgentRunResult(
            provider=kwargs["provider_name"],
            model="m",
            goal=kwargs["goal"],
            workflow_profile="fast",
            status=self.status,
            # A step never reports max_steps_reached: that is the run running out of steps after acting.
            steps=[_step(kwargs, "acted" if self.status == "max_steps_reached" else self.status, self._error())],
            final_session={"id": kwargs["session_id"]},
        )


class AgentJobErrorStatusTests(unittest.IsolatedAsyncioTestCase):
    async def _finished(self, status: str, kind: str) -> dict:
        with tempfile.TemporaryDirectory() as root:
            queue = AgentJobQueue(
                orchestrator=_Orchestrator(status),
                store_root=Path(root),
                session_accessible=every_session_usable,
                worker_count=1,
            )
            await queue.startup()
            try:
                if kind == "run":
                    job = await queue.enqueue_run("s1", AgentRunRequest(provider="openai", goal="g", max_steps=2))
                else:
                    job = await queue.enqueue_step("s1", AgentStepRequest(provider="openai", goal="g"))
                for _ in range(200):
                    stored = await queue.get_job(job["id"])
                    if stored["status"] in {"completed", "failed"}:
                        return stored
                    await asyncio.sleep(0.02)
                raise AssertionError("the job did not finish")
            finally:
                await queue.shutdown()

    async def test_an_errored_run_and_step_are_recorded_failed_with_the_reason(self) -> None:
        for kind in ("run", "step"):
            with self.subTest(kind=kind):
                stored = await self._finished("error", kind)
                self.assertEqual(stored["status"], "failed")
                self.assertEqual(stored["error"], ERROR)
                self.assertEqual(stored["result"]["status"], "error")

    async def test_other_outcomes_stay_completed(self) -> None:
        outcomes = (
            ("run", "done"),
            ("run", "takeover"),
            ("run", "approval_required"),
            ("run", "max_steps_reached"),
            ("step", "done"),
            ("step", "acted"),
        )
        for kind, status in outcomes:
            with self.subTest(kind=kind, status=status):
                stored = await self._finished(status, kind)
                self.assertEqual(stored["status"], "completed")
                self.assertIsNone(stored["error"])


class _RaisingOrchestrator:
    """An orchestrator whose step and run raise, as a fault outside the provider call does (a log file that cannot
    be written)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def step(self, **kwargs):
        raise self.exc

    async def run(self, **kwargs):
        raise self.exc


class AgentJobEscapedErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_error_that_escapes_the_run_puts_no_path_in_the_jobs_record(self) -> None:
        # STORE-27 (D1-7): the record said "PermissionError: [Errno 13] Permission denied: '/data/artifacts/..'", to
        # anyone who lists the jobs.
        path = "/data/artifacts/s1/agent_runs.jsonl"
        for kind in ("run", "step"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                queue = AgentJobQueue(
                    orchestrator=_RaisingOrchestrator(PermissionError(13, "Permission denied", path)),
                    store_root=Path(root),
                    session_accessible=every_session_usable,
                    worker_count=1,
                )
                await queue.startup()
                try:
                    with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
                        if kind == "run":
                            job = await queue.enqueue_run("s1", AgentRunRequest(provider="openai", goal="g"))
                        else:
                            job = await queue.enqueue_step("s1", AgentStepRequest(provider="openai", goal="g"))
                        for _ in range(200):
                            stored = await queue.get_job(job["id"])
                            if stored["status"] == "failed":
                                break
                            await asyncio.sleep(0.02)
                        else:
                            raise AssertionError("the job did not fail")
                finally:
                    await queue.shutdown()

                self.assertEqual(stored["error"], "Agent job failed")
                self.assertNotIn(path, json.dumps(stored))
                # The detail stays in the log, with the traceback.
                self.assertIn(path, " ".join(logged.output))
                self.assertIsNotNone(logged.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
