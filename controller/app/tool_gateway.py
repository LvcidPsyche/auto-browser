from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from .action_errors import BrowserActionError
from .approvals import ApprovalRequiredError
from .models import (
    AgentRunRequest,
    AgentStepRequest,
    ApprovalDecisionRequest,
    BrowserActionDecision,
    CreateSessionRequest,
    McpToolCallContent,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolDescriptor,
)
from .social_errors import SocialActionError


class EmptyInput(BaseModel):
    pass


class SessionIdInput(BaseModel):
    session_id: str


class ObserveInput(SessionIdInput):
    limit: int = Field(default=40, ge=1, le=100)


class ScreenshotInput(SessionIdInput):
    label: str = Field(default="manual", min_length=1, max_length=120)


class ExecuteActionInput(SessionIdInput):
    approval_id: str | None = None
    action: BrowserActionDecision


class SaveAuthStateInput(SessionIdInput):
    path: str


class SaveAuthProfileInput(SessionIdInput):
    profile_name: str = Field(min_length=1, max_length=120)


class TakeoverInput(SessionIdInput):
    reason: str = "Manual review requested"


class ListDownloadsInput(SessionIdInput):
    pass


class AuthProfileNameInput(BaseModel):
    profile_name: str = Field(min_length=1, max_length=120)


class ListAuthProfilesInput(BaseModel):
    pass


class ListTabsInput(SessionIdInput):
    pass


class TabActionInput(SessionIdInput):
    index: int = Field(ge=0)


class ApprovalIdInput(BaseModel):
    approval_id: str


class ApprovalDecisionInput(ApprovalIdInput):
    comment: str | None = Field(default=None, max_length=2000)


class ListApprovalsInput(BaseModel):
    status: str | None = None
    session_id: str | None = None


class ListAgentJobsInput(BaseModel):
    status: str | None = None
    session_id: str | None = None


class GetRemoteAccessInput(BaseModel):
    session_id: str | None = None


class AgentJobIdInput(BaseModel):
    job_id: str


class QueueAgentStepInput(SessionIdInput):
    request: AgentStepRequest


class QueueAgentRunInput(SessionIdInput):
    request: AgentRunRequest


class SocialScrollInput(SessionIdInput):
    direction: Literal["down", "up"] = "down"
    screens: int = Field(default=3, ge=1, le=20)


class SocialScrapeInput(SessionIdInput):
    limit: int = Field(default=20, ge=1, le=100)


class SocialPostInput(SessionIdInput):
    text: str = Field(min_length=1, max_length=5000)
    approval_id: str | None = None


class SocialCommentInput(SessionIdInput):
    text: str = Field(min_length=1, max_length=5000)
    post_index: int = Field(default=0, ge=0, le=50)
    approval_id: str | None = None


class SocialLikeInput(SessionIdInput):
    post_index: int = Field(default=0, ge=0, le=50)
    approval_id: str | None = None


class SocialFollowInput(SessionIdInput):
    approval_id: str | None = None


class SocialUnfollowInput(SessionIdInput):
    approval_id: str | None = None


class SocialRepostInput(SessionIdInput):
    post_index: int = Field(default=0, ge=0, le=50)
    approval_id: str | None = None


class SocialDmInput(SessionIdInput):
    recipient: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000)
    approval_id: str | None = None


class SocialLoginInput(SessionIdInput):
    platform: Literal["x", "twitter", "instagram", "linkedin", "outlook", "microsoft", "live"]
    username: str = Field(min_length=1, max_length=500)
    password: str = Field(min_length=1, max_length=5000, repr=False)
    auth_profile: str | None = Field(default=None, max_length=120)
    approval_id: str | None = None
    totp_secret: str | None = Field(default=None, repr=False)


class SocialSearchInput(SessionIdInput):
    query: str = Field(min_length=1, max_length=500)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[dict[str, Any] | list[dict[str, Any]]]]
    profiles: tuple[str, ...] = ("curated", "full")


class McpToolGateway:
    def __init__(self, *, manager, orchestrator, job_queue, tool_profile: str = "curated"):
        self.manager = manager
        self.orchestrator = orchestrator
        self.job_queue = job_queue
        self.tool_profile = "full" if tool_profile == "full" else "curated"
        self._tools = {
            spec.name: spec
            for spec in [
                ToolSpec(
                    name="browser.create_session",
                    description="Create a new browser session and optionally navigate to a start URL.",
                    input_model=CreateSessionRequest,
                    handler=self._create_session,
                ),
                ToolSpec(
                    name="browser.list_sessions",
                    description="List live and persisted browser sessions.",
                    input_model=EmptyInput,
                    handler=self._list_sessions,
                ),
                ToolSpec(
                    name="browser.get_session",
                    description="Get one browser session summary.",
                    input_model=SessionIdInput,
                    handler=self._get_session,
                ),
                ToolSpec(
                    name="browser.observe",
                    description="Capture the current browser observation with screenshot, interactables, and perception summary.",
                    input_model=ObserveInput,
                    handler=self._observe,
                ),
                ToolSpec(
                    name="browser.screenshot",
                    description="Capture a lightweight screenshot for one session without the full observe payload.",
                    input_model=ScreenshotInput,
                    handler=self._screenshot,
                ),
                ToolSpec(
                    name="browser.list_auth_profiles",
                    description="List reusable saved auth profiles that can be loaded into a new session.",
                    input_model=ListAuthProfilesInput,
                    handler=self._list_auth_profiles,
                ),
                ToolSpec(
                    name="browser.get_auth_profile",
                    description="Inspect one saved auth profile and its storage-state metadata.",
                    input_model=AuthProfileNameInput,
                    handler=self._get_auth_profile,
                ),
                ToolSpec(
                    name="browser.list_downloads",
                    description="List files captured from browser downloads for one session.",
                    input_model=ListDownloadsInput,
                    handler=self._list_downloads,
                ),
                ToolSpec(
                    name="browser.list_tabs",
                    description="List currently open tabs/pages for one session.",
                    input_model=ListTabsInput,
                    handler=self._list_tabs,
                ),
                ToolSpec(
                    name="browser.activate_tab",
                    description="Switch the active session page to one tab index.",
                    input_model=TabActionInput,
                    handler=self._activate_tab,
                ),
                ToolSpec(
                    name="browser.close_tab",
                    description="Close one tab index if more than one tab is open.",
                    input_model=TabActionInput,
                    handler=self._close_tab,
                ),
                ToolSpec(
                    name="browser.execute_action",
                    description="Execute one browser action using the shared internal action schema.",
                    input_model=ExecuteActionInput,
                    handler=self._execute_action,
                ),
                ToolSpec(
                    name="browser.save_auth_state",
                    description="Save session storage state to the per-session auth-state root.",
                    input_model=SaveAuthStateInput,
                    handler=self._save_auth_state,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.save_auth_profile",
                    description="Save the current session storage state into a reusable named auth profile.",
                    input_model=SaveAuthProfileInput,
                    handler=self._save_auth_profile,
                ),
                ToolSpec(
                    name="browser.request_human_takeover",
                    description="Ask for a human to take over the shared browser desktop.",
                    input_model=TakeoverInput,
                    handler=self._takeover,
                ),
                ToolSpec(
                    name="browser.close_session",
                    description="Close a session and finalize its trace/artifacts.",
                    input_model=SessionIdInput,
                    handler=self._close_session,
                ),
                ToolSpec(
                    name="browser.list_approvals",
                    description="List pending or historical approval items.",
                    input_model=ListApprovalsInput,
                    handler=self._list_approvals,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.approve_approval",
                    description="Approve a pending approval item.",
                    input_model=ApprovalDecisionInput,
                    handler=self._approve_approval,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.reject_approval",
                    description="Reject a pending approval item.",
                    input_model=ApprovalDecisionInput,
                    handler=self._reject_approval,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.execute_approval",
                    description="Execute an already approved action.",
                    input_model=ApprovalIdInput,
                    handler=self._execute_approval,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.list_agent_jobs",
                    description="List queued or completed browser-agent jobs.",
                    input_model=ListAgentJobsInput,
                    handler=self._list_agent_jobs,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.get_agent_job",
                    description="Read one browser-agent job record.",
                    input_model=AgentJobIdInput,
                    handler=self._get_agent_job,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.queue_agent_step",
                    description="Queue one agent step for background execution.",
                    input_model=QueueAgentStepInput,
                    handler=self._queue_agent_step,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.queue_agent_run",
                    description="Queue a short agent loop for background execution.",
                    input_model=QueueAgentRunInput,
                    handler=self._queue_agent_run,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.list_providers",
                    description="List configured model providers for browser-agent orchestration.",
                    input_model=EmptyInput,
                    handler=self._list_providers,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.get_remote_access",
                    description="Read current remote-access metadata for takeover/API forwarding.",
                    input_model=GetRemoteAccessInput,
                    handler=self._get_remote_access,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.scroll_feed",
                    description="Smoothly scroll the current page feed up or down by N screens using human-paced motion.",
                    input_model=SocialScrollInput,
                    handler=self._social_scroll,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.extract_posts",
                    description="Scrape visible feed posts from the current page. Returns structured list of {text, links, images, y_position}.",
                    input_model=SocialScrapeInput,
                    handler=self._social_extract_posts,
                ),
                ToolSpec(
                    name="social.extract_comments",
                    description="Scrape visible comments/replies from the current post page.",
                    input_model=SocialScrapeInput,
                    handler=self._social_extract_comments,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.extract_profile",
                    description="Extract profile info (username, bio, followers, following, avatar) from the current page.",
                    input_model=SessionIdInput,
                    handler=self._social_extract_profile,
                ),
                ToolSpec(
                    name="social.post",
                    description=(
                        "Find the text composer on the current page (tweet box, post field, comment box) "
                        "and type + submit the provided text. Navigate to the platform first. "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialPostInput,
                    handler=self._social_post,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.comment",
                    description=(
                        "Reply/comment on a visible post. Use post_index to target a specific post. "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialCommentInput,
                    handler=self._social_comment,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.like",
                    description=(
                        "Find and click the like/heart button for a visible post. "
                        "Use post_index to target a specific post (0 = first). "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialLikeInput,
                    handler=self._social_like,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.follow",
                    description=(
                        "Find and click the Follow button on the current profile page. "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialFollowInput,
                    handler=self._social_follow,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.unfollow",
                    description=(
                        "Find and click the unfollow/following button on the current profile page. "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialUnfollowInput,
                    handler=self._social_unfollow,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.repost",
                    description=(
                        "Find and click the repost/retweet button for a visible post. "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialRepostInput,
                    handler=self._social_repost,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.dm",
                    description=(
                        "Send a direct message on the current supported social platform. "
                        "If approval_id is omitted, this returns an approval_required error."
                    ),
                    input_model=SocialDmInput,
                    handler=self._social_dm,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="social.login",
                    description="Navigate to the platform login flow, enter credentials, handle TOTP if configured, and save auth state.",
                    input_model=SocialLoginInput,
                    handler=self._social_login,
                ),
                ToolSpec(
                    name="social.search",
                    description="Find the search input on the current page and type a query, then press Enter.",
                    input_model=SocialSearchInput,
                    handler=self._social_search,
                ),
            ]
            if self.tool_profile in spec.profiles
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            McpToolDescriptor(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_model.model_json_schema(),
            ).model_dump()
            for spec in self._tools.values()
        ]

    async def call_tool(self, payload: McpToolCallRequest) -> McpToolCallResponse:
        spec = self._tools.get(payload.name)
        if spec is None:
            return self._error_response(f"Unknown tool: {payload.name}")

        try:
            arguments = spec.input_model.model_validate(payload.arguments)
            result = await spec.handler(arguments)
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(result, ensure_ascii=False))],
                structuredContent=result,
                isError=False,
            )
        except ApprovalRequiredError as exc:
            detail = exc.payload
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(detail, ensure_ascii=False))],
                structuredContent=detail,
                isError=True,
            )
        except SocialActionError as exc:
            detail = exc.payload
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(detail, ensure_ascii=False))],
                structuredContent=detail,
                isError=True,
            )
        except BrowserActionError as exc:
            detail = exc.payload
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(detail, ensure_ascii=False))],
                structuredContent=detail,
                isError=True,
            )
        except Exception as exc:
            return self._error_response(str(exc))

    @staticmethod
    def _error_response(message: str) -> McpToolCallResponse:
        return McpToolCallResponse(
            content=[McpToolCallContent(text=message)],
            structuredContent={"error": message},
            isError=True,
        )

    async def _create_session(self, payload: CreateSessionRequest) -> dict[str, Any]:
        return await self.manager.create_session(
            name=payload.name,
            start_url=payload.start_url,
            storage_state_path=payload.storage_state_path,
            auth_profile=payload.auth_profile,
            request_proxy_server=payload.proxy_server,
            request_proxy_username=payload.proxy_username,
            request_proxy_password=payload.proxy_password,
            user_agent=payload.user_agent,
            totp_secret=payload.totp_secret,
        )

    async def _list_sessions(self, _: EmptyInput) -> list[dict[str, Any]]:
        return await self.manager.list_sessions()

    async def _get_session(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.get_session_record(payload.session_id)

    async def _observe(self, payload: ObserveInput) -> dict[str, Any]:
        return await self.manager.observe(payload.session_id, limit=payload.limit)

    async def _screenshot(self, payload: ScreenshotInput) -> dict[str, Any]:
        return await self.manager.capture_screenshot(payload.session_id, label=payload.label)

    async def _list_auth_profiles(self, _: ListAuthProfilesInput) -> list[dict[str, Any]]:
        return await self.manager.list_auth_profiles()

    async def _get_auth_profile(self, payload: AuthProfileNameInput) -> dict[str, Any]:
        return await self.manager.get_auth_profile(payload.profile_name)

    async def _list_downloads(self, payload: ListDownloadsInput) -> list[dict[str, Any]]:
        return await self.manager.list_downloads(payload.session_id)

    async def _list_tabs(self, payload: ListTabsInput) -> list[dict[str, Any]]:
        return await self.manager.list_tabs(payload.session_id)

    async def _activate_tab(self, payload: TabActionInput) -> dict[str, Any]:
        return await self.manager.activate_tab(payload.session_id, payload.index)

    async def _close_tab(self, payload: TabActionInput) -> dict[str, Any]:
        return await self.manager.close_tab(payload.session_id, payload.index)

    async def _execute_action(self, payload: ExecuteActionInput) -> dict[str, Any]:
        return await self.manager.execute_decision(
            payload.session_id,
            payload.action,
            approval_id=payload.approval_id,
        )

    async def _save_auth_state(self, payload: SaveAuthStateInput) -> dict[str, Any]:
        return await self.manager.save_storage_state(payload.session_id, payload.path)

    async def _save_auth_profile(self, payload: SaveAuthProfileInput) -> dict[str, Any]:
        return await self.manager.save_auth_profile(payload.session_id, payload.profile_name)

    async def _takeover(self, payload: TakeoverInput) -> dict[str, Any]:
        return await self.manager.request_human_takeover(payload.session_id, payload.reason)

    async def _close_session(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.close_session(payload.session_id)

    async def _list_approvals(self, payload: ListApprovalsInput) -> list[dict[str, Any]]:
        return await self.manager.list_approvals(status=payload.status, session_id=payload.session_id)

    async def _approve_approval(self, payload: ApprovalDecisionInput) -> dict[str, Any]:
        return await self.manager.approve(payload.approval_id, comment=payload.comment)

    async def _reject_approval(self, payload: ApprovalDecisionInput) -> dict[str, Any]:
        return await self.manager.reject(payload.approval_id, comment=payload.comment)

    async def _execute_approval(self, payload: ApprovalIdInput) -> dict[str, Any]:
        return await self.manager.execute_approval(payload.approval_id)

    async def _list_agent_jobs(self, payload: ListAgentJobsInput) -> list[dict[str, Any]]:
        return await self.job_queue.list_jobs(status=payload.status, session_id=payload.session_id)

    async def _get_agent_job(self, payload: AgentJobIdInput) -> dict[str, Any]:
        return await self.job_queue.get_job(payload.job_id)

    async def _queue_agent_step(self, payload: QueueAgentStepInput) -> dict[str, Any]:
        await self.manager.get_session(payload.session_id)
        return await self.job_queue.enqueue_step(payload.session_id, payload.request)

    async def _queue_agent_run(self, payload: QueueAgentRunInput) -> dict[str, Any]:
        await self.manager.get_session(payload.session_id)
        return await self.job_queue.enqueue_run(payload.session_id, payload.request)

    async def _list_providers(self, _: EmptyInput) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.orchestrator.list_providers()]

    async def _get_remote_access(self, payload: GetRemoteAccessInput) -> dict[str, Any]:
        if payload.session_id and payload.session_id not in self.manager.sessions:
            record = await self.manager.get_session_record(payload.session_id)
            return record["remote_access"]
        return self.manager.get_remote_access_info(payload.session_id)

    async def _social_scroll(self, payload: SocialScrollInput) -> dict[str, Any]:
        return await self.manager.scroll_feed(
            payload.session_id,
            direction=payload.direction,
            screens=payload.screens,
        )

    async def _social_extract_posts(self, payload: SocialScrapeInput) -> list[dict[str, Any]]:
        return await self.manager.extract_posts(payload.session_id, limit=payload.limit)

    async def _social_extract_comments(self, payload: SocialScrapeInput) -> list[dict[str, Any]]:
        return await self.manager.extract_comments(payload.session_id, limit=payload.limit)

    async def _social_extract_profile(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.extract_profile(payload.session_id)

    async def _social_post(self, payload: SocialPostInput) -> dict[str, Any]:
        return await self.manager.post_content(
            payload.session_id,
            text=payload.text,
            approval_id=payload.approval_id,
        )

    async def _social_comment(self, payload: SocialCommentInput) -> dict[str, Any]:
        return await self.manager.comment_on_post(
            payload.session_id,
            text=payload.text,
            post_index=payload.post_index,
            approval_id=payload.approval_id,
        )

    async def _social_like(self, payload: SocialLikeInput) -> dict[str, Any]:
        return await self.manager.like_post(
            payload.session_id,
            post_index=payload.post_index,
            approval_id=payload.approval_id,
        )

    async def _social_follow(self, payload: SocialFollowInput) -> dict[str, Any]:
        return await self.manager.follow_user(payload.session_id, approval_id=payload.approval_id)

    async def _social_unfollow(self, payload: SocialUnfollowInput) -> dict[str, Any]:
        return await self.manager.unfollow_user(payload.session_id, approval_id=payload.approval_id)

    async def _social_repost(self, payload: SocialRepostInput) -> dict[str, Any]:
        return await self.manager.repost_post(
            payload.session_id,
            post_index=payload.post_index,
            approval_id=payload.approval_id,
        )

    async def _social_dm(self, payload: SocialDmInput) -> dict[str, Any]:
        return await self.manager.send_direct_message(
            payload.session_id,
            recipient=payload.recipient,
            text=payload.text,
            approval_id=payload.approval_id,
        )

    async def _social_login(self, payload: SocialLoginInput) -> dict[str, Any]:
        return await self.manager.social_login(
            payload.session_id,
            platform=payload.platform,
            username=payload.username,
            password=payload.password,
            auth_profile=payload.auth_profile,
            approval_id=payload.approval_id,
            totp_secret=payload.totp_secret,
        )

    async def _social_search(self, payload: SocialSearchInput) -> dict[str, Any]:
        return await self.manager.search_page(payload.session_id, query=payload.query)
