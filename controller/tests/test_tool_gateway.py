from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.memory_manager import MemoryProfile
from app.metrics import MetricsRecorder
from app.models import ApprovalRecord, BrowserActionDecision, McpToolCallRequest, ProviderInfo
from app.tool_gateway import CreateSessionRequest, McpToolGateway, ToolRegistry, ToolSpec
from app.tool_inputs import EmptyInput


class ToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "session-1"}),
            list_sessions=AsyncMock(return_value=[{"id": "session-1"}]),
            get_session_record=AsyncMock(return_value={"id": "session-1", "status": "active"}),
            observe=AsyncMock(return_value={"session": {"id": "session-1"}, "url": "https://example.com"}),
            capture_screenshot=AsyncMock(return_value={"screenshot_url": "/artifacts/session-1/manual.png"}),
            get_console_messages=AsyncMock(return_value={"items": [{"type": "error", "text": "boom"}]}),
            get_page_errors=AsyncMock(return_value={"items": ["ReferenceError: nope"]}),
            get_request_failures=AsyncMock(
                return_value={"items": [{"url": "https://example.com/api", "failure": "net::ERR_FAILED"}]}
            ),
            stop_trace=AsyncMock(
                return_value={
                    "trace_path": "/data/artifacts/session-1/trace.zip",
                    "trace_url": "/artifacts/session-1/trace.zip",
                    "trace_exists": True,
                    "trace_recording": False,
                }
            ),
            list_auth_profiles=AsyncMock(return_value=[{"profile_name": "outlook-default"}]),
            get_auth_profile=AsyncMock(return_value={"profile_name": "outlook-default"}),
            list_tabs=AsyncMock(return_value=[{"index": 0, "active": True, "url": "https://example.com"}]),
            activate_tab=AsyncMock(return_value={"index": 1, "tabs": [{"index": 1, "active": True}]}),
            close_tab=AsyncMock(return_value={"closed_index": 1, "tabs": [{"index": 0, "active": True}]}),
            list_downloads=AsyncMock(return_value=[{"filename": "report.csv"}]),
            execute_decision=AsyncMock(return_value={"action": "click", "verification": {"verified": True}}),
            require_governed_approval=AsyncMock(return_value=None),
            save_storage_state=AsyncMock(return_value={"saved_to": "/data/auth/session-1/state.json.enc"}),
            save_auth_profile=AsyncMock(return_value={"profile_name": "outlook-default"}),
            request_human_takeover=AsyncMock(return_value={"takeover_url": "http://127.0.0.1:6080/vnc.html"}),
            close_session=AsyncMock(return_value={"closed": True}),
            list_approvals=AsyncMock(return_value=[]),
            approve=AsyncMock(return_value={"id": "approval-1", "status": "approved"}),
            reject=AsyncMock(return_value={"id": "approval-1", "status": "rejected"}),
            execute_approval=AsyncMock(return_value={"approval": {"id": "approval-1", "status": "executed"}}),
            get_remote_access_info=lambda: {"active": False, "status": "inactive"},
            get_session=AsyncMock(return_value={"id": "session-1"}),
            verify_witness_chain=AsyncMock(
                return_value={
                    "scope": "session-1",
                    "valid": True,
                    "receipt_count": 2,
                    "head_hash": "abc123",
                    "first_invalid_index": None,
                    "first_invalid_receipt_id": None,
                    "reason": None,
                }
            ),
            settings=SimpleNamespace(
                auth_state_encryption_key=None,
                require_auth_state_encryption=False,
                require_operator_id=False,
                api_bearer_token=None,
                session_isolation_mode="shared_browser_node",
                witness_enabled=True,
                witness_remote_url=None,
                allowed_hosts="*",
                pii_scrub_enabled=True,
                require_approval_for_uploads=True,
            ),
            memory=SimpleNamespace(
                save=AsyncMock(
                    return_value=MemoryProfile(
                        name="checkout",
                        created_at="2026-01-01T00:00:00Z",
                        updated_at="2026-01-01T00:00:00Z",
                        goal_summary="Buy the thing",
                    )
                ),
                get=AsyncMock(
                    return_value=MemoryProfile(
                        name="checkout",
                        created_at="2026-01-01T00:00:00Z",
                        updated_at="2026-01-01T00:00:00Z",
                        goal_summary="Buy the thing",
                    )
                ),
                list=AsyncMock(return_value=[{"name": "checkout", "step_count": 0}]),
                delete=AsyncMock(return_value=True),
            ),
            approvals=SimpleNamespace(mark_executed=AsyncMock()),
        )
        self.orchestrator = SimpleNamespace(
            list_providers=lambda: [ProviderInfo(provider="openai", configured=True, model="gpt-4.1-mini")]
        )
        self.job_queue = SimpleNamespace(
            list_jobs=AsyncMock(return_value=[]),
            get_job=AsyncMock(return_value={"id": "job-1", "status": "completed"}),
            resume_job=AsyncMock(return_value={"id": "job-2", "parent_job_id": "job-1", "status": "queued"}),
            discard_job=AsyncMock(return_value={"id": "job-1", "status": "discarded"}),
            cancel_job=AsyncMock(return_value={"id": "job-1", "status": "cancelled"}),
            enqueue_step=AsyncMock(return_value={"id": "job-1", "kind": "agent_step"}),
            enqueue_run=AsyncMock(return_value={"id": "job-2", "kind": "agent_run"}),
        )
        harness_record = SimpleNamespace(model_dump=lambda mode="json": {"id": "run-1", "status": "converged"})
        self.harness_service = SimpleNamespace(
            start_convergence=AsyncMock(return_value=harness_record),
            get_status=lambda run_id: {"id": run_id, "status": "converged"},
            get_trace=lambda run_id, attempt_index=None: {
                "run_id": run_id,
                "attempt_index": attempt_index,
                "final_observation": {"url": "https://example.com"},
            },
            list_runs=lambda status=None, limit=50: [{"id": "run-1", "status": status or "converged", "limit": limit}],
            list_candidates=lambda: [{"skill_id": "candidate-1"}],
            get_candidate=lambda skill_id: {"skill_id": skill_id},
            check_drift=AsyncMock(return_value={"skill_id": "candidate-1", "status": "healthy"}),
            check_all_drifts=AsyncMock(return_value=[{"skill_id": "candidate-1", "status": "healthy"}]),
            graduate=lambda run_id: {"status": "staged", "candidate": {"skill_id": run_id}},
        )
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
        )
        self.full_gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
            tool_profile="full",
            harness_service=self.harness_service,
        )
        self.vision_gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
            vision_targeter=object(),
        )

    async def test_list_tools_includes_expected_browser_tools(self) -> None:
        tools = self.gateway.list_tools()
        names = {tool["name"] for tool in tools}

        self.assertIn("browser.create_session", names)
        self.assertIn("browser.screenshot", names)
        self.assertIn("browser.get_console", names)
        self.assertIn("browser.get_page_errors", names)
        self.assertIn("browser.get_request_failures", names)
        self.assertIn("browser.stop_trace", names)
        self.assertIn("browser.save_memory_profile", names)
        self.assertIn("browser.get_memory_profile", names)
        self.assertIn("browser.list_memory_profiles", names)
        self.assertIn("browser.readiness_check", names)
        self.assertIn("browser.verify_witness", names)
        self.assertIn("browser.list_auth_profiles", names)
        self.assertIn("browser.get_auth_profile", names)
        self.assertIn("browser.list_tabs", names)
        self.assertIn("browser.list_downloads", names)
        self.assertIn("browser.execute_action", names)
        self.assertIn("browser.save_auth_profile", names)
        self.assertNotIn("browser.list_agent_jobs", names)
        self.assertNotIn("browser.resume_agent_job", names)
        self.assertNotIn("browser.list_providers", names)
        self.assertNotIn("browser.get_remote_access", names)
        self.assertNotIn("browser.list_approvals", names)
        self.assertNotIn("social.post", names)
        self.assertNotIn("social.comment", names)
        self.assertNotIn("social.like", names)
        self.assertNotIn("social.follow", names)
        self.assertNotIn("social.unfollow", names)
        self.assertNotIn("social.repost", names)
        self.assertNotIn("social.dm", names)
        self.assertNotIn("social.login", names)
        self.assertNotIn("social.search", names)
        self.assertNotIn("browser.find_by_vision", names)
        self.assertEqual(len(names), len(tools))
        self.assertNotIn("browser.discard_agent_job", names)
        self.assertNotIn("browser.cancel_agent_job", names)

    async def test_verify_witness_tool_dispatches_to_manager(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.verify_witness", arguments={"session_id": "session-1"})
        )

        self.assertFalse(response.isError)
        self.assertTrue(response.structuredContent["valid"])
        self.assertEqual(response.structuredContent["receipt_count"], 2)
        self.manager.verify_witness_chain.assert_awaited_once_with("session-1")

    async def test_list_tools_include_mcp_hints(self) -> None:
        tools = {tool["name"]: tool for tool in self.gateway.list_tools()}
        console_hints = tools["browser.get_console"]["annotations"]
        action_hints = tools["browser.execute_action"]["annotations"]

        self.assertEqual(
            console_hints,
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )
        self.assertEqual(
            action_hints,
            {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        )

    async def test_full_profile_tool_hints_classify_destructive_and_closed_domain_tools(self) -> None:
        tools = {tool["name"]: tool for tool in self.full_gateway.list_tools()}

        self.assertEqual(
            tools["browser.close_session"]["annotations"],
            {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        )
        self.assertEqual(
            tools["harness.list_runs"]["annotations"],
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )

    async def test_tool_gateway_package_reexports_legacy_input_models(self) -> None:
        payload = CreateSessionRequest(name="reexport-check")

        self.assertEqual(payload.name, "reexport-check")

    async def test_tool_registry_descriptor_cache_invalidates_and_returns_json_safe_copies(self) -> None:
        registry = ToolRegistry(tool_profile="curated", experimental_enabled=lambda _: True)

        async def handler(_: EmptyInput):
            return {}

        registry.register(
            ToolSpec(
                name="test.one",
                description="First tool.",
                input_model=EmptyInput,
                handler=handler,
                read_only_hint=True,
                idempotent_hint=True,
            )
        )
        first = registry.list_tools()
        first[0]["annotations"]["readOnlyHint"] = False

        self.assertTrue(registry.list_tools()[0]["annotations"]["readOnlyHint"])

        registry.register(
            ToolSpec(
                name="test.two",
                description="Second tool.",
                input_model=EmptyInput,
                handler=handler,
            )
        )

        self.assertEqual({tool["name"] for tool in registry.list_tools()}, {"test.one", "test.two"})

    async def test_gateway_records_bounded_mcp_tool_metrics(self) -> None:
        metrics = MetricsRecorder()
        gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
            metrics=metrics,
        )

        ok_response = await gateway.call_tool(McpToolCallRequest(name="browser.list_sessions", arguments={}))
        unknown_response = await gateway.call_tool(McpToolCallRequest(name="browser.not_real", arguments={}))

        payload, _ = metrics.render()
        text = payload.decode("utf-8")
        self.assertFalse(ok_response.isError)
        self.assertTrue(unknown_response.isError)
        ok_payload = ok_response.model_dump(exclude_none=True, by_alias=True)
        self.assertEqual(ok_payload["_meta"]["tool"], "browser.list_sessions")
        self.assertEqual(ok_payload["_meta"]["status"], "ok")
        self.assertGreaterEqual(ok_payload["_meta"]["latency_ms"], 0)
        self.assertEqual(unknown_response.meta["tool"], "__unknown__")
        self.assertIn('auto_browser_mcp_tool_calls_total{status="ok",tool="browser.list_sessions"} 1.0', text)
        self.assertIn('auto_browser_mcp_tool_calls_total{status="error",tool="__unknown__"} 1.0', text)
        self.assertNotIn("browser.not_real", text)

    async def test_full_profile_keeps_internal_tools_available(self) -> None:
        names = {tool["name"] for tool in self.full_gateway.list_tools()}

        self.assertIn("browser.list_agent_jobs", names)
        self.assertIn("browser.resume_agent_job", names)
        self.assertIn("browser.discard_agent_job", names)
        self.assertIn("browser.cancel_agent_job", names)
        self.assertIn("browser.list_providers", names)
        self.assertIn("browser.delete_memory_profile", names)
        self.assertIn("browser.readiness_check", names)
        self.assertIn("browser.list_approvals", names)
        self.assertIn("harness.start_convergence", names)
        self.assertIn("harness.get_status", names)
        self.assertIn("harness.get_trace", names)
        self.assertIn("harness.list_runs", names)
        self.assertIn("harness.list_candidates", names)
        self.assertIn("harness.get_candidate", names)
        self.assertIn("harness.check_drift", names)
        self.assertIn("harness.check_all_drifts", names)
        self.assertIn("harness.graduate", names)
        self.assertNotIn("social.post", names)
        self.assertNotIn("social.dm", names)
        self.assertNotIn("browser.find_by_vision", names)

    async def test_social_tools_are_not_shipped_even_with_full_profile(self) -> None:
        gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
            tool_profile="full",
        )
        names = {tool["name"] for tool in gateway.list_tools()}
        self.assertNotIn("social.post", names)
        self.assertNotIn("social.login", names)

        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="social.post",
                arguments={"session_id": "session-1", "text": "hello world"},
            )
        )

        self.assertTrue(response.isError)
        self.assertIn("Unknown tool: social.post", response.structuredContent["error"])

    async def test_harness_tools_are_full_profile_only_and_forward_arguments(self) -> None:
        curated_names = {tool["name"] for tool in self.gateway.list_tools()}
        self.assertNotIn("harness.start_convergence", curated_names)

        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="harness.start_convergence",
                arguments={
                    "contract": {
                        "id": "task-1",
                        "goal": "Reach the done page",
                        "postconditions": [{"kind": "url_contains", "value": "example.com/done"}],
                        "budget": {"max_attempts": 1, "max_steps": 2},
                    },
                    "mock_final_observation": {"url": "https://example.com/done"},
                    "max_attempts": 1,
                },
            )
        )
        status_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="harness.get_status", arguments={"run_id": "run-1"})
        )
        graduate_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="harness.graduate", arguments={"run_id": "run-1"})
        )
        candidates_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="harness.list_candidates", arguments={})
        )
        candidate_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="harness.get_candidate", arguments={"skill_id": "candidate-1"})
        )
        drift_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="harness.check_drift", arguments={"skill_id": "candidate-1"})
        )
        all_drift_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="harness.check_all_drifts", arguments={})
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["status"], "converged")
        self.assertFalse(status_response.isError)
        self.assertEqual(status_response.structuredContent["status"], "converged")
        self.assertFalse(graduate_response.isError)
        self.assertEqual(graduate_response.structuredContent["status"], "staged")
        self.assertFalse(candidates_response.isError)
        self.assertEqual(candidates_response.structuredContent[0]["skill_id"], "candidate-1")
        self.assertFalse(candidate_response.isError)
        self.assertEqual(candidate_response.structuredContent["skill_id"], "candidate-1")
        self.assertFalse(drift_response.isError)
        self.assertEqual(drift_response.structuredContent["status"], "healthy")
        self.assertFalse(all_drift_response.isError)
        self.assertEqual(all_drift_response.structuredContent[0]["status"], "healthy")
        self.harness_service.start_convergence.assert_awaited_once()
        self.harness_service.check_drift.assert_awaited_once_with("candidate-1")
        self.harness_service.check_all_drifts.assert_awaited_once_with()
        contract = self.harness_service.start_convergence.await_args.args[0]
        self.assertEqual(contract.id, "task-1")

    async def test_harness_tools_return_clear_error_when_service_unavailable(self) -> None:
        gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
            tool_profile="full",
        )

        response = await gateway.call_tool(McpToolCallRequest(name="harness.list_runs", arguments={}))

        self.assertTrue(response.isError)
        self.assertIn("harness service unavailable", response.structuredContent["error"])
        self.assertIn("HARNESS_*", response.structuredContent["error"])

    async def test_eval_js_requires_governed_profile(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="browser.eval_js",
                arguments={"session_id": "session-1", "expression": "() => 1"},
            )
        )

        self.assertTrue(response.isError)
        self.assertIn("requires workflow_profile=governed", response.structuredContent["error"])

    async def test_live_harness_start_requires_governed_profile(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="harness.start_convergence",
                arguments={
                    "session_id": "session-1",
                    "contract": {
                        "id": "task-live",
                        "goal": "Reach the done page",
                        "postconditions": [{"kind": "url_contains", "value": "done"}],
                        "budget": {"max_steps": 2, "max_attempts": 1},
                    },
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertIn("requires workflow_profile=governed", response.structuredContent["error"])
        self.harness_service.start_convergence.assert_not_awaited()

    async def test_resume_agent_job_tool_forwards_arguments(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="browser.resume_agent_job", arguments={"job_id": "job-1", "max_steps": 2})
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["parent_job_id"], "job-1")
        self.job_queue.resume_job.assert_awaited_once_with("job-1", max_steps=2)

    async def test_discard_agent_job_tool_forwards_arguments(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="browser.discard_agent_job", arguments={"job_id": "job-1"})
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["status"], "discarded")
        self.job_queue.discard_job.assert_awaited_once_with("job-1")

    async def test_cancel_agent_job_tool_forwards_arguments(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="browser.cancel_agent_job", arguments={"job_id": "job-1"})
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["status"], "cancelled")
        self.job_queue.cancel_job.assert_awaited_once_with("job-1")

    async def test_vision_tool_is_listed_when_targeter_is_available(self) -> None:
        names = {tool["name"] for tool in self.vision_gateway.list_tools()}

        self.assertIn("browser.find_by_vision", names)

    async def test_readiness_tool_returns_report(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.readiness_check", arguments={"mode": "confidential"})
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["mode"], "confidential")
        self.assertIn(response.structuredContent["overall"], {"warn", "fail"})

    async def test_memory_profile_tools_forward_arguments(self) -> None:
        save_response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.save_memory_profile",
                arguments={
                    "session_id": "session-1",
                    "profile_name": "checkout",
                    "goal_summary": "Buy the thing",
                    "completed_steps": ["opened cart"],
                    "discovered_selectors": {"buy": "#buy"},
                    "notes": ["requires login"],
                },
            )
        )
        get_response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.get_memory_profile", arguments={"profile_name": "checkout"})
        )
        list_response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.list_memory_profiles", arguments={})
        )
        delete_response = await self.full_gateway.call_tool(
            McpToolCallRequest(name="browser.delete_memory_profile", arguments={"profile_name": "checkout"})
        )

        self.assertFalse(save_response.isError)
        self.assertFalse(get_response.isError)
        self.assertFalse(list_response.isError)
        self.assertFalse(delete_response.isError)
        self.manager.get_session.assert_awaited_once_with("session-1")
        self.manager.memory.save.assert_awaited_once()
        self.manager.memory.get.assert_awaited_once_with("checkout")
        self.manager.memory.list.assert_awaited_once_with()
        self.manager.memory.delete.assert_awaited_once_with("checkout")

    async def test_execute_action_tool_returns_structured_payload(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": "session-1",
                    "action": {
                        "action": "click",
                        "reason": "Click the main CTA",
                        "element_id": "op-123",
                    },
                },
            )
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["action"], "click")
        self.manager.execute_decision.assert_awaited_once()
        called_args = self.manager.execute_decision.await_args.args
        self.assertEqual(called_args[0], "session-1")
        self.assertIsInstance(called_args[1], BrowserActionDecision)
        self.assertEqual(called_args[1].element_id, "op-123")

    async def test_governed_execute_action_requires_gateway_approval(self) -> None:
        approval = ApprovalRecord(
            id="approval-governed-tool-1",
            session_id="session-1",
            kind="write",
            status="pending",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
            reason="Governed MCP call requires approval",
            action=BrowserActionDecision(
                action="click",
                reason="Click save",
                element_id="op-save",
                risk_category="write",
            ),
        )
        self.manager.require_governed_approval = AsyncMock(side_effect=ApprovalRequiredError(approval))

        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": "session-1",
                    "workflow_profile": "governed",
                    "action": {
                        "action": "click",
                        "reason": "Click save",
                        "element_id": "op-save",
                    },
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["status"], "approval_required")
        self.manager.require_governed_approval.assert_awaited_once()
        self.manager.execute_decision.assert_not_awaited()

    async def test_governed_storage_tool_uses_approval_token_and_marks_executed(self) -> None:
        approval = ApprovalRecord(
            id="approval-governed-storage-1",
            session_id="session-1",
            kind="account_change",
            status="approved",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
            reason="Governed storage mutation approved",
            action=BrowserActionDecision(
                action="request_human_takeover",
                reason="Approve governed MCP tool call browser.set_local_storage",
                risk_category="account_change",
            ),
        )
        self.manager.require_governed_approval = AsyncMock(return_value=approval)
        page = SimpleNamespace(evaluate=AsyncMock())
        self.manager.get_session = AsyncMock(return_value=SimpleNamespace(page=page))

        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="browser.set_local_storage",
                arguments={
                    "session_id": "session-1",
                    "workflow_profile": "governed",
                    "approval_id": "approval-governed-storage-1",
                    "storage_type": "local",
                    "key": "theme",
                    "value": "dark",
                },
            )
        )

        self.assertFalse(response.isError)
        self.manager.require_governed_approval.assert_awaited_once()
        page.evaluate.assert_awaited_once()
        self.manager.approvals.mark_executed.assert_awaited_once_with("approval-governed-storage-1")

    async def test_auth_profile_tools_forward_arguments(self) -> None:
        list_response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.list_auth_profiles", arguments={})
        )
        get_response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.get_auth_profile", arguments={"profile_name": "outlook-default"})
        )
        save_response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.save_auth_profile",
                arguments={"session_id": "session-1", "profile_name": "outlook-default"},
            )
        )

        self.assertFalse(list_response.isError)
        self.assertFalse(get_response.isError)
        self.assertFalse(save_response.isError)
        self.manager.list_auth_profiles.assert_awaited_once()
        self.manager.get_auth_profile.assert_awaited_once_with("outlook-default")
        self.manager.save_auth_profile.assert_awaited_once_with("session-1", "outlook-default")

    async def test_screenshot_tool_forwards_arguments(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.screenshot",
                arguments={"session_id": "session-1", "label": "checkpoint"},
            )
        )

        self.assertFalse(response.isError)
        self.manager.capture_screenshot.assert_awaited_once_with("session-1", label="checkpoint")

    async def test_debug_tools_forward_arguments(self) -> None:
        console_response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.get_console",
                arguments={"session_id": "session-1", "limit": 5},
            )
        )
        page_error_response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.get_page_errors",
                arguments={"session_id": "session-1", "limit": 7},
            )
        )
        request_failure_response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.get_request_failures",
                arguments={"session_id": "session-1", "limit": 9},
            )
        )
        trace_response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.stop_trace",
                arguments={"session_id": "session-1"},
            )
        )

        self.assertFalse(console_response.isError)
        self.assertFalse(page_error_response.isError)
        self.assertFalse(request_failure_response.isError)
        self.assertFalse(trace_response.isError)
        self.manager.get_console_messages.assert_awaited_once_with("session-1", limit=5)
        self.manager.get_page_errors.assert_awaited_once_with("session-1", limit=7)
        self.manager.get_request_failures.assert_awaited_once_with("session-1", limit=9)
        self.manager.stop_trace.assert_awaited_once_with("session-1")

    async def test_create_session_forwards_proxy_and_user_agent_options(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.create_session",
                arguments={
                    "name": "session-1",
                    "start_url": "https://example.com",
                    "auth_profile": "outlook-default",
                    "proxy_server": "http://proxy.internal:8080",
                    "proxy_username": "alice",
                    "proxy_password": "secret",
                    "user_agent": "AutoBrowserTest/1.0",
                    "protection_mode": "confidential",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                },
            )
        )

        self.assertFalse(response.isError)
        self.manager.create_session.assert_awaited_once_with(
            name="session-1",
            start_url="https://example.com",
            storage_state_path=None,
            auth_profile="outlook-default",
            memory_profile=None,
            proxy_persona=None,
            request_proxy_server="http://proxy.internal:8080",
            request_proxy_username="alice",
            request_proxy_password="secret",
            user_agent="AutoBrowserTest/1.0",
            protection_mode="confidential",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

    async def test_create_session_forwards_proxy_persona(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.create_session",
                arguments={
                    "name": "session-1",
                    "start_url": "https://example.com",
                    "proxy_persona": "us-east",
                },
            )
        )

        self.assertFalse(response.isError)
        self.manager.create_session.assert_awaited_once_with(
            name="session-1",
            start_url="https://example.com",
            storage_state_path=None,
            auth_profile=None,
            memory_profile=None,
            proxy_persona="us-east",
            request_proxy_server=None,
            request_proxy_username=None,
            request_proxy_password=None,
            user_agent=None,
            protection_mode=None,
            totp_secret=None,
        )

    async def test_create_session_forwards_memory_profile(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.create_session",
                arguments={"memory_profile": "checkout"},
            )
        )

        self.assertFalse(response.isError)
        self.assertEqual(self.manager.create_session.await_args.kwargs["memory_profile"], "checkout")

    async def test_full_profile_extended_runtime_tools_forward_to_helpers(self) -> None:
        class FakeLocator:
            @property
            def first(self):
                return self

            async def bounding_box(self):
                return {"x": 10, "y": 20, "width": 30, "height": 40}

        class FakeMouse:
            def __init__(self) -> None:
                self.move = AsyncMock()
                self.down = AsyncMock()
                self.up = AsyncMock()

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://example.com"
                self.mouse = FakeMouse()
                self.evaluate = AsyncMock(side_effect=["eval-result", "plain text", [{"tag": "button"}], "stored", {"all": "items"}, None])
                self.wait_for_selector = AsyncMock()
                self.content = AsyncMock(return_value="<html></html>")
                self.set_viewport_size = AsyncMock()

            def locator(self, selector: str):
                return FakeLocator()

        fake_context = SimpleNamespace(
            cookies=AsyncMock(return_value=[{"name": "sid"}]),
            add_cookies=AsyncMock(),
        )
        fake_session = SimpleNamespace(
            page=FakePage(),
            context=fake_context,
            artifact_dir=Path("."),
        )
        self.manager.get_session = AsyncMock(return_value=fake_session)
        self.manager.get_network_log = AsyncMock(return_value={"entries": []})
        self.manager.fork_session = AsyncMock(return_value={"id": "fork-1"})
        self.manager.cdp_attach = AsyncMock(return_value={"attached": True})
        self.manager.enable_shadow_browse = AsyncMock(return_value={"enabled": True})
        self.manager.capture_screenshot = AsyncMock(return_value={"screenshot_path": "vision.png"})
        self.manager.get_pii_scrubber_status = lambda: {"enabled": True}
        self.manager.audit = object()
        self.manager.settings.default_viewport_width = 1280
        self.manager.settings.default_viewport_height = 720

        cron_service = SimpleNamespace(
            list_jobs=AsyncMock(return_value=[]),
            create_job=AsyncMock(return_value={"id": "cron-1"}),
            delete_job=AsyncMock(return_value=True),
            trigger_job=AsyncMock(return_value={"triggered": True}),
        )
        proxy_store = SimpleNamespace(
            list_personas=lambda: [{"name": "us-east"}],
            set_persona=lambda *args, **kwargs: {"name": args[0]},
            delete_persona=lambda name: True,
        )
        share_manager = SimpleNamespace(create_token=lambda session_id, ttl_seconds: {"token": "share-token"})
        vision_targeter = SimpleNamespace(find_element=AsyncMock(return_value={"selector": "button"}))
        gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
            tool_profile="full",
            cron_service=cron_service,
            share_manager=share_manager,
            proxy_store=proxy_store,
            vision_targeter=vision_targeter,
        )

        calls = [
            ("browser.get_network_log", {"session_id": "session-1"}),
            ("browser.fork_session", {"session_id": "session-1", "name": "fork"}),
            ("browser.eval_js", {"session_id": "session-1", "expression": "() => 1", "workflow_profile": "governed"}),
            ("browser.wait_for_selector", {"session_id": "session-1", "selector": "button"}),
            ("browser.get_html", {"session_id": "session-1", "text_only": True}),
            ("browser.get_html", {"session_id": "session-1", "text_only": False}),
            ("browser.find_elements", {"session_id": "session-1", "selector": "button"}),
            (
                "browser.drag_drop",
                {"session_id": "session-1", "source_selector": "#a", "target_selector": "#b"},
            ),
            ("browser.set_viewport", {"session_id": "session-1", "width": 800, "height": 600}),
            ("browser.get_cookies", {"session_id": "session-1", "urls": ["https://example.com"]}),
            ("browser.set_cookies", {"session_id": "session-1", "cookies": [{"name": "sid", "value": "1", "url": "https://example.com"}]}),
            ("browser.get_local_storage", {"session_id": "session-1", "key": "k"}),
            ("browser.get_local_storage", {"session_id": "session-1"}),
            ("browser.set_local_storage", {"session_id": "session-1", "key": "k", "value": "v"}),
            ("browser.cdp_attach", {"cdp_url": "http://127.0.0.1:9222"}),
            ("browser.find_by_vision", {"session_id": "session-1", "description": "submit", "take_screenshot": True}),
            ("browser.share_session", {"session_id": "session-1", "ttl_minutes": 5}),
            ("browser.enable_shadow_browse", {"session_id": "session-1"}),
            ("browser.list_proxy_personas", {}),
            ("browser.create_proxy_persona", {"name": "us-east", "server": "http://proxy.example.com:8080"}),
            ("browser.delete_proxy_persona", {"name": "us-east"}),
            ("browser.list_cron_jobs", {}),
            ("browser.create_cron_job", {"name": "daily", "goal": "check", "schedule": "0 9 * * *"}),
            ("browser.delete_cron_job", {"job_id": "cron-1"}),
            ("browser.trigger_cron_job", {"job_id": "cron-1"}),
            ("browser.pii_scrubber_status", {}),
        ]

        for name, arguments in calls:
            response = await gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))
            self.assertFalse(response.isError, name)

        self.manager.get_network_log.assert_awaited_once()
        self.manager.fork_session.assert_awaited_once()
        fake_session.page.wait_for_selector.assert_awaited_once()
        fake_context.add_cookies.assert_awaited_once()
        vision_targeter.find_element.assert_awaited_once()

    async def test_observe_tool_forwards_preset(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.observe",
                arguments={"session_id": "session-1", "preset": "rich", "limit": 50},
            )
        )

        self.assertFalse(response.isError)
        self.manager.observe.assert_awaited_once_with("session-1", limit=50, preset="rich")

    async def test_approval_required_bubbles_back_as_tool_error(self) -> None:
        approval = ApprovalRecord(
            id="approval-1",
            session_id="session-1",
            kind="payment",
            status="pending",
            created_at="2026-03-09T00:00:00Z",
            updated_at="2026-03-09T00:00:00Z",
            reason="Payment requires approval",
            action=BrowserActionDecision(
                action="click",
                reason="Submit payment",
                element_id="op-pay",
                risk_category="payment",
            ),
        )
        self.manager.execute_decision = AsyncMock(side_effect=ApprovalRequiredError(approval))

        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": "session-1",
                    "action": {
                        "action": "click",
                        "reason": "Submit payment",
                        "element_id": "op-pay",
                        "risk_category": "payment",
                    },
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["status"], "approval_required")
        self.assertEqual(response.structuredContent["approval"]["id"], "approval-1")

    async def test_browser_action_error_bubbles_back_as_structured_tool_error(self) -> None:
        self.manager.execute_decision = AsyncMock(
            side_effect=BrowserActionError(
                "Action failed",
                action="click",
                details={"snapshot": {"screenshot_url": "/artifacts/session-1/fail-click.png"}},
            )
        )

        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": "session-1",
                    "action": {
                        "action": "click",
                        "reason": "Click the button",
                        "element_id": "op-1",
                    },
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "browser_action_failed")
        self.assertEqual(
            response.structuredContent["snapshot"]["screenshot_url"],
            "/artifacts/session-1/fail-click.png",
        )
