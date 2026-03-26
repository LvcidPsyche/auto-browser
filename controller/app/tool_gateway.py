from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from .action_errors import BrowserActionError
from .approvals import ApprovalRequiredError
from .models import (
    McpToolCallContent,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolDescriptor,
)
from .social_errors import SocialActionError
from .tool_inputs import (  # noqa: F401 — re-exported for backwards compat
    AgentJobIdInput,
    AgentRunRequest,
    AgentStepRequest,
    ApprovalDecisionInput,
    ApprovalIdInput,
    AuthProfileNameInput,
    CdpAttachInput,
    CreateCronJobInput,
    CreateProxyPersonaInput,
    CreateSessionRequest,
    CronJobIdInput,
    DragDropInput,
    EmptyInput,
    EvalJsInput,
    ExecuteActionInput,
    ExportScriptInput,
    FindElementsInput,
    ForkCdpInput,
    ForkSessionInput,
    GetCookiesInput,
    GetNetworkLogInput,
    GetPageHtmlInput,
    GetRemoteAccessInput,
    GetStorageInput,
    ListAgentJobsInput,
    ListApprovalsInput,
    ListAuthProfilesInput,
    ListDownloadsInput,
    ListTabsInput,
    ObserveInput,
    ProxyPersonaNameInput,
    QueueAgentRunInput,
    QueueAgentStepInput,
    SaveAuthProfileInput,
    SaveAuthStateInput,
    ScreenshotInput,
    SessionIdInput,
    SessionTailInput,
    SetCookiesInput,
    SetStorageInput,
    SetViewportInput,
    ShadowBrowseInput,
    ShareSessionInput,
    SocialCommentInput,
    SocialDmInput,
    SocialFollowInput,
    SocialLikeInput,
    SocialLoginInput,
    SocialPostInput,
    SocialRepostInput,
    SocialScrapeInput,
    SocialScrollInput,
    SocialSearchInput,
    SocialUnfollowInput,
    TabActionInput,
    TakeoverInput,
    TriggerCronJobInput,
    ValidateShareTokenInput,
    VisionFindInput,
    WaitForSelectorInput,
)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[dict[str, Any] | list[dict[str, Any]]]]
    profiles: tuple[str, ...] = ("curated", "full")


class McpToolGateway:
    def __init__(
        self,
        *,
        manager,
        orchestrator,
        job_queue,
        tool_profile: str = "curated",
        cron_service=None,
        share_manager=None,
        proxy_store=None,
        vision_targeter=None,
    ):
        self.manager = manager
        self.orchestrator = orchestrator
        self.job_queue = job_queue
        self.tool_profile = "full" if tool_profile == "full" else "curated"
        self.cron_service = cron_service
        self.share_manager = share_manager
        self.proxy_store = proxy_store
        self.vision_targeter = vision_targeter
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
                    name="browser.get_console",
                    description="Read recent browser console messages for an active session.",
                    input_model=SessionTailInput,
                    handler=self._get_console,
                ),
                ToolSpec(
                    name="browser.get_page_errors",
                    description="Read recent uncaught page errors for an active session.",
                    input_model=SessionTailInput,
                    handler=self._get_page_errors,
                ),
                ToolSpec(
                    name="browser.get_request_failures",
                    description="Read recent failed network requests for an active session.",
                    input_model=SessionTailInput,
                    handler=self._get_request_failures,
                ),
                ToolSpec(
                    name="browser.stop_trace",
                    description="Finalize the current Playwright trace for an active session and return its artifact path.",
                    input_model=SessionIdInput,
                    handler=self._stop_trace,
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
                # ── v0.5.0: Network Inspector ──────────────────────────────
                ToolSpec(
                    name="browser.get_network_log",
                    description=(
                        "Return captured HTTP request/response entries for a session. "
                        "Filtered by method (GET/POST/...) or URL substring. "
                        "All sensitive headers and bodies are automatically PII-scrubbed."
                    ),
                    input_model=GetNetworkLogInput,
                    handler=self._get_network_log,
                ),
                # ── v0.5.0: Session Forking ────────────────────────────────
                ToolSpec(
                    name="browser.fork_session",
                    description=(
                        "Fork a session: snapshot its cookies, storage state, and current URL, "
                        "then create a new independent session with that state. "
                        "Useful for branching workflows or running parallel variants."
                    ),
                    input_model=ForkSessionInput,
                    handler=self._fork_session,
                ),
                # ── v0.5.0: DOM / JS Tools ─────────────────────────────────
                ToolSpec(
                    name="browser.eval_js",
                    description=(
                        "Execute a JavaScript expression in the current page context "
                        "and return the result. Use for DOM queries, value extraction, "
                        "or lightweight scripting that has no dedicated tool."
                    ),
                    input_model=EvalJsInput,
                    handler=self._eval_js,
                ),
                ToolSpec(
                    name="browser.wait_for_selector",
                    description=(
                        "Wait for a CSS selector to reach a specific state "
                        "(visible, hidden, attached, detached). "
                        "Returns when the condition is met or raises on timeout."
                    ),
                    input_model=WaitForSelectorInput,
                    handler=self._wait_for_selector,
                ),
                ToolSpec(
                    name="browser.get_html",
                    description=(
                        "Get the HTML source of the current page. "
                        "Set text_only=true to strip tags and return plain text. "
                        "Set full_page=false (default) for visible viewport only."
                    ),
                    input_model=GetPageHtmlInput,
                    handler=self._get_html,
                ),
                ToolSpec(
                    name="browser.find_elements",
                    description=(
                        "Find all elements matching a CSS selector and return their "
                        "text, href, value, bounding box, and visibility. "
                        "Useful before clicking or scraping multiple items."
                    ),
                    input_model=FindElementsInput,
                    handler=self._find_elements,
                ),
                ToolSpec(
                    name="browser.drag_drop",
                    description=(
                        "Drag from one element or coordinate to another. "
                        "Provide source_selector OR (source_x, source_y), "
                        "and target_selector OR (target_x, target_y)."
                    ),
                    input_model=DragDropInput,
                    handler=self._drag_drop,
                ),
                ToolSpec(
                    name="browser.set_viewport",
                    description=(
                        "Resize the browser viewport to the specified width and height."
                    ),
                    input_model=SetViewportInput,
                    handler=self._set_viewport,
                ),
                # ── v0.5.0: Cookies & Storage ──────────────────────────────
                ToolSpec(
                    name="browser.get_cookies",
                    description=(
                        "Get all cookies for the current session context. "
                        "Optionally filter by URL(s)."
                    ),
                    input_model=GetCookiesInput,
                    handler=self._get_cookies,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.set_cookies",
                    description=(
                        "Set one or more cookies in the current session context. "
                        "Each cookie dict must have at minimum: name, value, domain."
                    ),
                    input_model=SetCookiesInput,
                    handler=self._set_cookies,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.get_local_storage",
                    description=(
                        "Read a key (or all keys) from localStorage or sessionStorage "
                        "in the current page context."
                    ),
                    input_model=GetStorageInput,
                    handler=self._get_local_storage,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.set_local_storage",
                    description=(
                        "Write a key-value pair to localStorage or sessionStorage "
                        "in the current page context."
                    ),
                    input_model=SetStorageInput,
                    handler=self._set_local_storage,
                    profiles=("full",),
                ),
                # ── v0.5.0: Playwright Script Export ──────────────────────
                ToolSpec(
                    name="browser.export_script",
                    description=(
                        "Export the current session's recorded actions as a runnable "
                        "Playwright Python script. Returns the script as a string "
                        "that can be saved to a .py file and run standalone."
                    ),
                    input_model=ExportScriptInput,
                    handler=self._export_script,
                    profiles=("full",),
                ),
                # ── v0.5.0: CDP Attach ─────────────────────────────────────
                ToolSpec(
                    name="browser.cdp_attach",
                    description=(
                        "Attach to an already-running Chrome instance via CDP URL "
                        "(e.g. http://localhost:9222). "
                        "After attaching, new sessions will use pages from that browser. "
                        "This allows automation of a real browser with existing logins."
                    ),
                    input_model=CdpAttachInput,
                    handler=self._cdp_attach,
                    profiles=("full",),
                ),
                # ── v0.5.0: Vision-Grounded Targeting ─────────────────────
                ToolSpec(
                    name="browser.find_by_vision",
                    description=(
                        "Use Claude Vision to find an element from a natural language description. "
                        "Returns (x, y) coordinates you can pass to browser.execute_action click. "
                        "Use when CSS selectors fail or the element has no reliable text anchor."
                    ),
                    input_model=VisionFindInput,
                    handler=self._find_by_vision,
                ),
                # ── v0.5.0: Shared Session Links ───────────────────────────
                ToolSpec(
                    name="browser.share_session",
                    description=(
                        "Create a time-limited share token for a session. "
                        "Returns a signed token that grants read-only observation access. "
                        "Pass the token to a teammate or use with GET /share/{token}/observe."
                    ),
                    input_model=ShareSessionInput,
                    handler=self._share_session,
                    profiles=("full",),
                ),
                # ── v0.5.0: Shadow Browsing ────────────────────────────────
                ToolSpec(
                    name="browser.enable_shadow_browse",
                    description=(
                        "Switch a stuck session to headed (visible) mode for debugging. "
                        "Creates a new headful browser window with the same state and URL. "
                        "The agent can watch what's happening or a human can take over."
                    ),
                    input_model=ShadowBrowseInput,
                    handler=self._enable_shadow_browse,
                    profiles=("full",),
                ),
                # ── v0.5.0: Proxy Personas ─────────────────────────────────
                ToolSpec(
                    name="browser.list_proxy_personas",
                    description=(
                        "List all configured proxy personas. "
                        "Each persona assigns a named static IP/proxy to a session "
                        "to prevent platform fingerprinting across agents."
                    ),
                    input_model=EmptyInput,
                    handler=self._list_proxy_personas,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.create_proxy_persona",
                    description=(
                        "Create or update a named proxy persona with server URL and credentials. "
                        "Use the persona name in CreateSessionRequest.proxy_persona."
                    ),
                    input_model=CreateProxyPersonaInput,
                    handler=self._create_proxy_persona,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.delete_proxy_persona",
                    description="Delete a named proxy persona.",
                    input_model=ProxyPersonaNameInput,
                    handler=self._delete_proxy_persona,
                    profiles=("full",),
                ),
                # ── v0.5.0: Cron / Webhook Triggers ───────────────────────
                ToolSpec(
                    name="browser.list_cron_jobs",
                    description="List all configured cron / webhook trigger jobs.",
                    input_model=EmptyInput,
                    handler=self._list_cron_jobs,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.create_cron_job",
                    description=(
                        "Create a browser automation job that runs on a cron schedule "
                        "and/or via an HTTP webhook trigger. "
                        "The agent will pursue 'goal' for up to max_steps actions."
                    ),
                    input_model=CreateCronJobInput,
                    handler=self._create_cron_job,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.delete_cron_job",
                    description="Delete a cron / webhook trigger job.",
                    input_model=CronJobIdInput,
                    handler=self._delete_cron_job,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.trigger_cron_job",
                    description="Immediately trigger a cron job (internal — no webhook auth required).",
                    input_model=CronJobIdInput,
                    handler=self._trigger_cron_job,
                    profiles=("full",),
                ),
                # ── v0.5.0: PII Scrubber Status ───────────────────────────
                ToolSpec(
                    name="browser.pii_scrubber_status",
                    description=(
                        "Return the current PII scrubber configuration: which patterns "
                        "are active, which layers are enabled, and the replacement string."
                    ),
                    input_model=EmptyInput,
                    handler=self._pii_scrubber_status,
                    profiles=("full",),
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

    async def _get_console(self, payload: SessionTailInput) -> dict[str, Any]:
        return await self.manager.get_console_messages(payload.session_id, limit=payload.limit)

    async def _get_page_errors(self, payload: SessionTailInput) -> dict[str, Any]:
        return await self.manager.get_page_errors(payload.session_id, limit=payload.limit)

    async def _get_request_failures(self, payload: SessionTailInput) -> dict[str, Any]:
        return await self.manager.get_request_failures(payload.session_id, limit=payload.limit)

    async def _stop_trace(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.stop_trace(payload.session_id)

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

    # ── v0.5.0 handlers ────────────────────────────────────────────────────

    async def _get_network_log(self, payload: GetNetworkLogInput) -> dict[str, Any]:
        return await self.manager.get_network_log(
            payload.session_id,
            limit=payload.limit,
            method=payload.method,
            url_contains=payload.url_contains,
        )

    async def _fork_session(self, payload: ForkSessionInput) -> dict[str, Any]:
        return await self.manager.fork_session(
            payload.session_id,
            name=payload.name,
            start_url=payload.start_url,
        )

    async def _eval_js(self, payload: EvalJsInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        result = await session.page.evaluate(payload.expression)
        return {"session_id": payload.session_id, "result": result}

    async def _wait_for_selector(self, payload: WaitForSelectorInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        await session.page.wait_for_selector(
            payload.selector,
            timeout=payload.timeout_ms,
            state=payload.state,
        )
        return {"session_id": payload.session_id, "selector": payload.selector, "state": payload.state}

    async def _get_html(self, payload: GetPageHtmlInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        if payload.text_only:
            text = await session.page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            return {"session_id": payload.session_id, "content": text, "type": "text"}
        html = await session.page.content()
        return {"session_id": payload.session_id, "content": html, "type": "html"}

    async def _find_elements(self, payload: FindElementsInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        elements = await session.page.evaluate(
            """([selector, limit]) => {
                const els = [...document.querySelectorAll(selector)].slice(0, limit);
                return els.map(el => {
                    const r = el.getBoundingClientRect();
                    return {
                        tag: el.tagName.toLowerCase(),
                        text: el.innerText?.substring(0, 200) || '',
                        value: el.value || null,
                        href: el.href || null,
                        id: el.id || null,
                        class: el.className || null,
                        visible: r.width > 0 && r.height > 0,
                        x: Math.round(r.x), y: Math.round(r.y),
                        width: Math.round(r.width), height: Math.round(r.height),
                    };
                });
            }""",
            [payload.selector, payload.limit],
        )
        return {"session_id": payload.session_id, "selector": payload.selector, "elements": elements}

    async def _drag_drop(self, payload: DragDropInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)

        # Resolve source coordinates
        if payload.source_selector:
            box = await session.page.locator(payload.source_selector).first.bounding_box()
            sx = box["x"] + box["width"] / 2 if box else 0
            sy = box["y"] + box["height"] / 2 if box else 0
        elif payload.source_x is not None and payload.source_y is not None:
            sx, sy = payload.source_x, payload.source_y
        else:
            raise ValueError("Provide source_selector or source_x/source_y")

        # Resolve target coordinates
        if payload.target_selector:
            box = await session.page.locator(payload.target_selector).first.bounding_box()
            tx = box["x"] + box["width"] / 2 if box else 0
            ty = box["y"] + box["height"] / 2 if box else 0
        elif payload.target_x is not None and payload.target_y is not None:
            tx, ty = payload.target_x, payload.target_y
        else:
            raise ValueError("Provide target_selector or target_x/target_y")

        await session.page.mouse.move(sx, sy)
        await session.page.mouse.down()
        await session.page.mouse.move(tx, ty, steps=10)
        await session.page.mouse.up()
        return {"session_id": payload.session_id, "from": {"x": sx, "y": sy}, "to": {"x": tx, "y": ty}}

    async def _set_viewport(self, payload: SetViewportInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        await session.page.set_viewport_size({"width": payload.width, "height": payload.height})
        return {"session_id": payload.session_id, "width": payload.width, "height": payload.height}

    async def _get_cookies(self, payload: GetCookiesInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        cookies = await session.context.cookies(urls=payload.urls)
        return {"session_id": payload.session_id, "cookies": cookies}

    async def _set_cookies(self, payload: SetCookiesInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        await session.context.add_cookies(payload.cookies)
        return {"session_id": payload.session_id, "set": len(payload.cookies)}

    async def _get_local_storage(self, payload: GetStorageInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        if payload.key:
            script = f"() => window.{payload.storage_type}Storage.getItem({payload.key!r})"
            value = await session.page.evaluate(script)
            return {"session_id": payload.session_id, "key": payload.key, "value": value}
        script = (
            f"() => Object.fromEntries("
            f"Object.keys(window.{payload.storage_type}Storage).map("
            f"k => [k, window.{payload.storage_type}Storage.getItem(k)]))"
        )
        data = await session.page.evaluate(script)
        return {"session_id": payload.session_id, "storage": data}

    async def _set_local_storage(self, payload: SetStorageInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        script = f"([k, v]) => window.{payload.storage_type}Storage.setItem(k, v)"
        await session.page.evaluate(script, [payload.key, payload.value])
        return {"session_id": payload.session_id, "key": payload.key, "set": True}

    async def _export_script(self, payload: ExportScriptInput) -> dict[str, Any]:
        from .playwright_export import export_session_script
        session = await self.manager.get_session(payload.session_id)
        start_url = session.page.url
        return await export_session_script(
            payload.session_id,
            self.manager.audit,
            start_url=start_url,
            viewport_w=self.manager.settings.default_viewport_width,
            viewport_h=self.manager.settings.default_viewport_height,
        )

    async def _cdp_attach(self, payload: CdpAttachInput) -> dict[str, Any]:
        return await self.manager.cdp_attach(payload.cdp_url)

    async def _find_by_vision(self, payload: VisionFindInput) -> dict[str, Any]:
        if self.vision_targeter is None:
            raise RuntimeError(
                "Vision targeting is not available — set ANTHROPIC_API_KEY to enable it."
            )
        session = await self.manager.get_session(payload.session_id)
        if payload.take_screenshot:
            screenshot = await self.manager.capture_screenshot(payload.session_id, label="vision")
            screenshot_path = screenshot["screenshot_path"]
        else:
            # Use the most recent screenshot if available
            screenshots = sorted(
                session.artifact_dir.glob("*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not screenshots:
                raise RuntimeError("No screenshots available — take one first")
            screenshot_path = str(screenshots[0])

        result = await self.vision_targeter.find_element(screenshot_path, payload.description)
        return {"session_id": payload.session_id, **result}

    async def _share_session(self, payload: ShareSessionInput) -> dict[str, Any]:
        if self.share_manager is None:
            raise RuntimeError("Session sharing is not configured")
        await self.manager.get_session(payload.session_id)  # verify session exists
        return self.share_manager.create_token(
            payload.session_id,
            ttl_seconds=payload.ttl_minutes * 60,
        )

    async def _enable_shadow_browse(self, payload: ShadowBrowseInput) -> dict[str, Any]:
        return await self.manager.enable_shadow_browse(payload.session_id)

    async def _list_proxy_personas(self, _: EmptyInput) -> list[dict[str, Any]]:
        if self.proxy_store is None:
            return []
        return self.proxy_store.list_personas()

    async def _create_proxy_persona(self, payload: CreateProxyPersonaInput) -> dict[str, Any]:
        if self.proxy_store is None:
            raise RuntimeError("No PROXY_PERSONA_FILE configured")
        return self.proxy_store.set_persona(
            payload.name,
            server=payload.server,
            username=payload.username,
            password=payload.password,
            description=payload.description,
        )

    async def _delete_proxy_persona(self, payload: ProxyPersonaNameInput) -> dict[str, Any]:
        if self.proxy_store is None:
            raise RuntimeError("No PROXY_PERSONA_FILE configured")
        deleted = self.proxy_store.delete_persona(payload.name)
        return {"name": payload.name, "deleted": deleted}

    async def _list_cron_jobs(self, _: EmptyInput) -> list[dict[str, Any]]:
        if self.cron_service is None:
            return []
        return await self.cron_service.list_jobs()

    async def _create_cron_job(self, payload: CreateCronJobInput) -> dict[str, Any]:
        if self.cron_service is None:
            raise RuntimeError("Cron service not initialized")
        return await self.cron_service.create_job(
            name=payload.name,
            goal=payload.goal,
            schedule=payload.schedule,
            start_url=payload.start_url,
            auth_profile=payload.auth_profile,
            proxy_persona=payload.proxy_persona,
            max_steps=payload.max_steps,
            enabled=payload.enabled,
            webhook_enabled=payload.webhook_enabled,
        )

    async def _delete_cron_job(self, payload: CronJobIdInput) -> dict[str, Any]:
        if self.cron_service is None:
            raise RuntimeError("Cron service not initialized")
        deleted = await self.cron_service.delete_job(payload.job_id)
        return {"job_id": payload.job_id, "deleted": deleted}

    async def _trigger_cron_job(self, payload: CronJobIdInput) -> dict[str, Any]:
        if self.cron_service is None:
            raise RuntimeError("Cron service not initialized")
        return await self.cron_service.trigger_job(payload.job_id)

    async def _pii_scrubber_status(self, _: EmptyInput) -> dict[str, Any]:
        return self.manager.get_pii_scrubber_status()
