from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.models import ApprovalRecord, BrowserActionDecision, McpToolCallRequest, ProviderInfo
from app.social_errors import SocialActionError
from app.tool_gateway import McpToolGateway


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
            save_storage_state=AsyncMock(return_value={"saved_to": "/data/auth/session-1/state.json.enc"}),
            save_auth_profile=AsyncMock(return_value={"profile_name": "outlook-default"}),
            request_human_takeover=AsyncMock(return_value={"takeover_url": "http://127.0.0.1:6080/vnc.html"}),
            close_session=AsyncMock(return_value={"closed": True}),
            scroll_feed=AsyncMock(return_value={"action": "scroll_feed"}),
            extract_posts=AsyncMock(return_value=[{"text": "hello"}]),
            extract_comments=AsyncMock(return_value=[{"text": "reply"}]),
            extract_profile=AsyncMock(return_value={"username": "@example"}),
            post_content=AsyncMock(return_value={"action": "social_post"}),
            comment_on_post=AsyncMock(return_value={"action": "social_comment"}),
            like_post=AsyncMock(return_value={"action": "social_like"}),
            follow_user=AsyncMock(return_value={"action": "social_follow"}),
            unfollow_user=AsyncMock(return_value={"action": "social_unfollow"}),
            repost_post=AsyncMock(return_value={"action": "social_repost"}),
            send_direct_message=AsyncMock(return_value={"action": "social_dm"}),
            social_login=AsyncMock(return_value={"action": "social_login"}),
            search_page=AsyncMock(return_value={"action": "social_search"}),
            list_approvals=AsyncMock(return_value=[]),
            approve=AsyncMock(return_value={"id": "approval-1", "status": "approved"}),
            reject=AsyncMock(return_value={"id": "approval-1", "status": "rejected"}),
            execute_approval=AsyncMock(return_value={"approval": {"id": "approval-1", "status": "executed"}}),
            get_remote_access_info=lambda: {"active": False, "status": "inactive"},
            get_session=AsyncMock(return_value={"id": "session-1"}),
        )
        self.orchestrator = SimpleNamespace(
            list_providers=lambda: [ProviderInfo(provider="openai", configured=True, model="gpt-4.1-mini")]
        )
        self.job_queue = SimpleNamespace(
            list_jobs=AsyncMock(return_value=[]),
            get_job=AsyncMock(return_value={"id": "job-1", "status": "completed"}),
            enqueue_step=AsyncMock(return_value={"id": "job-1", "kind": "agent_step"}),
            enqueue_run=AsyncMock(return_value={"id": "job-2", "kind": "agent_run"}),
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
        self.assertIn("browser.list_auth_profiles", names)
        self.assertIn("browser.get_auth_profile", names)
        self.assertIn("browser.list_tabs", names)
        self.assertIn("browser.list_downloads", names)
        self.assertIn("browser.execute_action", names)
        self.assertIn("browser.save_auth_profile", names)
        self.assertNotIn("browser.list_agent_jobs", names)
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
        self.assertIn("social.login", names)
        self.assertIn("social.search", names)
        self.assertEqual(len(names), 32)
        self.assertEqual(len(names), len(tools))

    async def test_full_profile_keeps_internal_tools_available(self) -> None:
        names = {tool["name"] for tool in self.full_gateway.list_tools()}

        self.assertIn("browser.list_agent_jobs", names)
        self.assertIn("browser.list_providers", names)
        self.assertIn("browser.list_approvals", names)
        self.assertIn("social.post", names)
        self.assertIn("social.dm", names)

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
            request_proxy_server="http://proxy.internal:8080",
            request_proxy_username="alice",
            request_proxy_password="secret",
            user_agent="AutoBrowserTest/1.0",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

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

    async def test_social_post_tool_bubbles_approval_back_as_tool_error(self) -> None:
        approval = ApprovalRecord(
            id="approval-social-1",
            session_id="session-1",
            kind="post",
            status="pending",
            created_at="2026-03-09T00:00:00Z",
            updated_at="2026-03-09T00:00:00Z",
            reason="Posting requires approval",
            action=BrowserActionDecision(
                action="social_post",
                reason="Publish a social post",
                text="hello world",
                risk_category="post",
            ),
        )
        self.manager.post_content = AsyncMock(side_effect=ApprovalRequiredError(approval))

        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="social.post",
                arguments={
                    "session_id": "session-1",
                    "text": "hello world",
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["status"], "approval_required")
        self.assertEqual(response.structuredContent["approval"]["kind"], "post")

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

    async def test_social_post_tool_forwards_approval_id(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="social.post",
                arguments={
                    "session_id": "session-1",
                    "text": "hello world",
                    "approval_id": "approval-social-1",
                },
            )
        )

        self.assertFalse(response.isError)
        self.manager.post_content.assert_awaited_once_with(
            "session-1",
            text="hello world",
            approval_id="approval-social-1",
        )

    async def test_social_comment_tool_forwards_fields(self) -> None:
        response = await self.full_gateway.call_tool(
            McpToolCallRequest(
                name="social.comment",
                arguments={
                    "session_id": "session-1",
                    "text": "great update",
                    "post_index": 2,
                    "approval_id": "approval-social-2",
                },
            )
        )

        self.assertFalse(response.isError)
        self.manager.comment_on_post.assert_awaited_once_with(
            "session-1",
            text="great update",
            post_index=2,
            approval_id="approval-social-2",
        )

    async def test_social_login_tool_forwards_credentials_and_approval(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="social.login",
                arguments={
                    "session_id": "session-1",
                    "platform": "x",
                    "username": "alice",
                    "password": "secret-password",
                    "auth_profile": "outlook-default",
                    "approval_id": "approval-login-1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                },
            )
        )

        self.assertFalse(response.isError)
        self.manager.social_login.assert_awaited_once_with(
            "session-1",
            platform="x",
            username="alice",
            password="secret-password",
            auth_profile="outlook-default",
            approval_id="approval-login-1",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

    async def test_social_errors_return_structured_tool_error(self) -> None:
        self.manager.social_login = AsyncMock(
            side_effect=SocialActionError(
                "captcha detected",
                action="social_login",
                code="captcha_detected",
                retryable=False,
                url="https://x.com/i/flow/login",
                details={"signal": "captcha"},
            )
        )

        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="social.login",
                arguments={
                    "session_id": "session-1",
                    "platform": "x",
                    "username": "alice",
                    "password": "secret-password",
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "captcha_detected")
        self.assertEqual(response.structuredContent["action"], "social_login")
