"""A job whose run or step reports an error is recorded failed, not completed."""

from __future__ import annotations

import asyncio
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
            steps=[_step(kwargs, self.status, self._error())],
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
        for kind, status in (("run", "done"), ("run", "takeover"), ("run", "approval_required"), ("step", "done")):
            with self.subTest(kind=kind, status=status):
                stored = await self._finished(status, kind)
                self.assertEqual(stored["status"], "completed")
                self.assertIsNone(stored["error"])


if __name__ == "__main__":
    unittest.main()
