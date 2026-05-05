from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models import BrowserActionDecision
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision


class RepeatingAdapter:
    default_model = "test-model"

    async def decide(self, **kwargs):
        return ProviderDecision(
            provider="openai",
            model="test-model",
            decision=BrowserActionDecision(
                action="click",
                reason="Click the same thing again",
                element_id="op-repeat",
                risk_category="write",
            ),
            usage={"provider": "fake"},
            raw_text='{"action":"click"}',
        )


class StaticRegistry:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def get(self, name):
        return self.adapter

    def list(self):
        return []


class BrowserOrchestratorLoopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        artifact_dir = Path(self.tempdir.name)
        self.session = SimpleNamespace(artifact_dir=artifact_dir)
        self.manager = SimpleNamespace(
            observe=AsyncMock(
                return_value={
                    "url": "https://example.com",
                    "title": "Example",
                    "text_excerpt": "same page",
                    "screenshot_path": str(artifact_dir / "screen.png"),
                }
            ),
            execute_decision=AsyncMock(
                return_value={
                    "before": {"url": "https://example.com", "title": "Example", "text_excerpt": "same page"},
                    "after": {"url": "https://example.com", "title": "Example", "text_excerpt": "same page"},
                    "verification": {"verified": False, "signals": []},
                }
            ),
            request_human_takeover=AsyncMock(return_value={"takeover_url": "http://127.0.0.1:6080/vnc.html"}),
            get_session=AsyncMock(return_value=self.session),
            get_session_summary=AsyncMock(return_value={"id": "session-1", "status": "active"}),
            _append_jsonl=AsyncMock(),
            _session_summary=AsyncMock(return_value={"id": "session-1", "status": "active"}),
        )
        self.orchestrator = BrowserOrchestrator(self.manager, StaticRegistry(RepeatingAdapter()))

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_run_triggers_takeover_after_repeated_low_progress_steps(self) -> None:
        result = await self.orchestrator.run(
            session_id="session-1",
            provider_name="openai",
            goal="Keep clicking until something happens",
            max_steps=6,
        )

        self.assertEqual(result.status, "takeover")
        self.assertEqual(result.steps[-1].status, "takeover")
        self.assertEqual(result.steps[-1].decision["action"], "request_human_takeover")
        self.manager.request_human_takeover.assert_awaited_once()
        self.assertEqual(self.manager.execute_decision.await_count, 3)

    async def test_step_prefixes_goal_with_memory_context(self) -> None:
        class MemoryAwareAdapter:
            default_model = "test-model"

            def __init__(self) -> None:
                self.last_goal = ""

            async def decide(self, **kwargs):
                self.last_goal = kwargs["goal"]
                return ProviderDecision(
                    provider="openai",
                    model="test-model",
                    decision=BrowserActionDecision(
                        action="done",
                        reason="Enough context",
                        risk_category="read",
                    ),
                    usage={"provider": "fake"},
                    raw_text='{"action":"done"}',
                )

        adapter = MemoryAwareAdapter()
        self.session.metadata = {"memory_context": "[Memory: checkout]\nKnown selector: #buy"}
        orchestrator = BrowserOrchestrator(self.manager, StaticRegistry(adapter))

        result = await orchestrator.step(
            session_id="session-1",
            provider_name="openai",
            goal="Click the buy button",
        )

        self.assertEqual(result.status, "done")
        self.assertIn("[Memory: checkout]", adapter.last_goal)
        self.assertIn("Current goal: Click the buy button", adapter.last_goal)

    async def test_governed_workflow_profile_adds_conservative_context(self) -> None:
        class ContextAwareAdapter:
            default_model = "test-model"

            def __init__(self) -> None:
                self.last_context_hints = ""

            async def decide(self, **kwargs):
                self.last_context_hints = kwargs["context_hints"]
                return ProviderDecision(
                    provider="openai",
                    model="test-model",
                    decision=BrowserActionDecision(
                        action="done",
                        reason="Enough context",
                        risk_category="read",
                    ),
                    usage={"provider": "fake"},
                    raw_text='{"action":"done"}',
                )

        adapter = ContextAwareAdapter()
        orchestrator = BrowserOrchestrator(self.manager, StaticRegistry(adapter))

        result = await orchestrator.step(
            session_id="session-1",
            provider_name="openai",
            goal="Inspect the account page",
            context_hints="Stay on the current account.",
            workflow_profile="governed",
        )

        self.assertEqual(result.workflow_profile, "governed")
        self.assertIn("Stay on the current account.", adapter.last_context_hints)
        self.assertIn("Workflow profile: governed", adapter.last_context_hints)
        self.assertIn("request human takeover", adapter.last_context_hints)


if __name__ == "__main__":
    unittest.main()
