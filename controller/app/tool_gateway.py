from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from .action_errors import BrowserActionError
from .approvals import ApprovalRequiredError
from .models import (
    BrowserActionDecision,
    McpToolCallContent,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolDescriptor,
)
from .readiness import run_readiness_checks
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
    DeleteMemoryProfileInput,
    DragDropInput,
    EmptyInput,
    EvalJsInput,
    ExecuteActionInput,
    ExportScriptInput,
    FindElementsInput,
    ForkCdpInput,
    ForkSessionInput,
    GetCookiesInput,
    GetMemoryProfileInput,
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
    ReadinessCheckInput,
    ResumeAgentJobInput,
    SaveAuthProfileInput,
    SaveAuthStateInput,
    SaveMemoryProfileInput,
    ScreenshotInput,
    SessionIdInput,
    SessionTailInput,
    SetCookiesInput,
    SetStorageInput,
    SetViewportInput,
    ShadowBrowseInput,
    ShareSessionInput,
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
    experimental: str | None = None
    governed_kind: str | None = None


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
                    name="browser.save_memory_profile",
                    description=(
                        "Save a named memory profile with context from the current session. "
                        "Loaded into future sessions via memory_profile=name in create_session."
                    ),
                    input_model=SaveMemoryProfileInput,
                    handler=self._save_memory_profile,
                    governed_kind="write",
                ),
                ToolSpec(
                    name="browser.get_memory_profile",
                    description="Retrieve a saved memory profile by name.",
                    input_model=GetMemoryProfileInput,
                    handler=self._get_memory_profile,
                ),
                ToolSpec(
                    name="browser.list_memory_profiles",
                    description="List all saved memory profiles.",
                    input_model=EmptyInput,
                    handler=self._list_memory_profiles,
                ),
                ToolSpec(
                    name="browser.delete_memory_profile",
                    description="Delete a named memory profile.",
                    input_model=DeleteMemoryProfileInput,
                    handler=self._delete_memory_profile,
                    profiles=("full",),
                    governed_kind="destructive",
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
                    governed_kind="write",
                ),
                ToolSpec(
                    name="browser.execute_action",
                    description="Execute one browser action using the shared internal action schema.",
                    input_model=ExecuteActionInput,
                    handler=self._execute_action,
                    governed_kind="dynamic",
                ),
                ToolSpec(
                    name="browser.save_auth_state",
                    description="Save session storage state to the per-session auth-state root.",
                    input_model=SaveAuthStateInput,
                    handler=self._save_auth_state,
                    profiles=("full",),
                    governed_kind="account_change",
                ),
                ToolSpec(
                    name="browser.save_auth_profile",
                    description="Save the current session storage state into a reusable named auth profile.",
                    input_model=SaveAuthProfileInput,
                    handler=self._save_auth_profile,
                    governed_kind="account_change",
                ),
                ToolSpec(
                    name="browser.request_human_takeover",
                    description="Ask for a human to take over the shared browser desktop.",
                    input_model=TakeoverInput,
                    handler=self._takeover,
                    governed_kind="write",
                ),
                ToolSpec(
                    name="browser.close_session",
                    description="Close a session and finalize its trace/artifacts.",
                    input_model=SessionIdInput,
                    handler=self._close_session,
                    governed_kind="write",
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
                    name="browser.resume_agent_job",
                    description="Resume an interrupted, failed, or step-limited background agent run from checkpoints.",
                    input_model=ResumeAgentJobInput,
                    handler=self._resume_agent_job,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.discard_agent_job",
                    description="Discard a queued or finished background agent job so operators can clear stale work.",
                    input_model=AgentJobIdInput,
                    handler=self._discard_agent_job,
                    profiles=("full",),
                ),
                ToolSpec(
                    name="browser.cancel_agent_job",
                    description="Cancel a queued or running background agent job.",
                    input_model=AgentJobIdInput,
                    handler=self._cancel_agent_job,
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
                    name="browser.readiness_check",
                    description=(
                        "Run a deployment readiness check. Returns pass/warn/fail for encryption, "
                        "operator identity, bearer token, session isolation, Witness audit, "
                        "host allowlist, PII scrubbing, and upload approval. "
                        "Pass mode='confidential' for stricter checks."
                    ),
                    input_model=ReadinessCheckInput,
                    handler=self._readiness_check,
                ),
                # ── Network Inspector ──────────────────────────────────────
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
                # ── Session Forking ────────────────────────────────────────
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
                # ── DOM / JS Tools ─────────────────────────────────────────
                ToolSpec(
                    name="browser.eval_js",
                    description=(
                        "Execute a JavaScript expression in the current page context "
                        "and return the result. Use for DOM queries, value extraction, "
                        "or lightweight scripting that has no dedicated tool."
                    ),
                    input_model=EvalJsInput,
                    handler=self._eval_js,
                    governed_kind="write",
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
                    governed_kind="write",
                ),
                ToolSpec(
                    name="browser.set_viewport",
                    description=(
                        "Resize the browser viewport to the specified width and height."
                    ),
                    input_model=SetViewportInput,
                    handler=self._set_viewport,
                ),
                # ── Cookies & Storage ──────────────────────────────────────
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
                    governed_kind="account_change",
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
                    governed_kind="account_change",
                ),
                # ── Playwright Script Export ───────────────────────────────
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
                # ── CDP Attach ─────────────────────────────────────────────
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
                    governed_kind="account_change",
                ),
                # ── Vision-Grounded Targeting ─────────────────────────────
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
                # ── Shared Session Links ───────────────────────────────────
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
                    governed_kind="write",
                ),
                # ── Shadow Browsing ────────────────────────────────────────
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
                    governed_kind="write",
                ),
                # ── Proxy Personas ─────────────────────────────────────────
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
                # ── Cron / Webhook Triggers ────────────────────────────────
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
                # ── PII Scrubber Status ────────────────────────────────────
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
            if self.tool_profile in spec.profiles and self._experimental_enabled(spec.experimental)
        }
        if vision_targeter is None and "browser.find_by_vision" in self._tools:
            del self._tools["browser.find_by_vision"]

    def _experimental_enabled(self, name: str | None) -> bool:
        return name is None

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
            raw_arguments = dict(payload.arguments or {})
            policy_profile = self._pop_policy_profile(spec, raw_arguments)
            policy_approval_id = self._pop_policy_approval_id(spec, raw_arguments)
            arguments = spec.input_model.model_validate(raw_arguments)
            approval = await self._require_governed_tool_approval(
                spec,
                arguments,
                workflow_profile=policy_profile,
                approval_id=policy_approval_id,
            )
            result = await spec.handler(arguments)
            if approval is not None:
                await self.manager.approvals.mark_executed(approval.id)
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
        except BrowserActionError as exc:
            detail = exc.payload
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(detail, ensure_ascii=False))],
                structuredContent=detail,
                isError=True,
            )
        except Exception:
            return self._error_response("Tool execution failed")

    @staticmethod
    def _error_response(message: str) -> McpToolCallResponse:
        return McpToolCallResponse(
            content=[McpToolCallContent(text=message)],
            structuredContent={"error": message},
            isError=True,
        )

    @staticmethod
    def _pop_policy_profile(spec: ToolSpec, raw_arguments: dict[str, Any]) -> str:
        profile = str(raw_arguments.pop("policy_profile", "") or raw_arguments.get("workflow_profile") or "fast")
        if "workflow_profile" not in spec.input_model.model_fields:
            raw_arguments.pop("workflow_profile", None)
        return profile

    @staticmethod
    def _pop_policy_approval_id(spec: ToolSpec, raw_arguments: dict[str, Any]) -> str | None:
        approval_id = raw_arguments.get("approval_id")
        if "approval_id" not in spec.input_model.model_fields:
            approval_id = raw_arguments.pop("approval_id", approval_id)
        governed_approval_id = raw_arguments.pop("governed_approval_id", None)
        return str(governed_approval_id or approval_id) if governed_approval_id or approval_id else None

    async def _require_governed_tool_approval(
        self,
        spec: ToolSpec,
        arguments: BaseModel,
        *,
        workflow_profile: str,
        approval_id: str | None,
    ):
        if workflow_profile != "governed" or spec.governed_kind is None:
            return None
        session_id = getattr(arguments, "session_id", None)
        if not session_id:
            return None
        decision = getattr(arguments, "action", None)
        if not isinstance(decision, BrowserActionDecision):
            decision = BrowserActionDecision(
                action="request_human_takeover",
                reason=f"Approve governed MCP tool call {spec.name}",
                risk_category=spec.governed_kind if spec.governed_kind != "dynamic" else "write",
            )
        return await self.manager.require_governed_approval(
            session_id,
            decision,
            approval_id=approval_id,
        )

    async def _create_session(self, payload: CreateSessionRequest) -> dict[str, Any]:
        return await self.manager.create_session(
            name=payload.name,
            start_url=payload.start_url,
            storage_state_path=payload.storage_state_path,
            auth_profile=payload.auth_profile,
            memory_profile=payload.memory_profile,
            proxy_persona=payload.proxy_persona,
            request_proxy_server=payload.proxy_server,
            request_proxy_username=payload.proxy_username,
            request_proxy_password=payload.proxy_password,
            user_agent=payload.user_agent,
            protection_mode=payload.protection_mode,
            totp_secret=payload.totp_secret,
        )

    async def _list_sessions(self, _: EmptyInput) -> list[dict[str, Any]]:
        return await self.manager.list_sessions()

    async def _save_memory_profile(self, payload: SaveMemoryProfileInput) -> dict[str, Any]:
        if self.manager.memory is None:
            raise RuntimeError("Memory profiles are not enabled.")
        await self.manager.get_session(payload.session_id)
        profile = await self.manager.memory.save(
            payload.profile_name,
            goal_summary=payload.goal_summary,
            completed_steps=payload.completed_steps,
            discovered_selectors=payload.discovered_selectors,
            notes=payload.notes,
            metadata={"session_id": payload.session_id},
        )
        return profile.model_dump()

    async def _get_memory_profile(self, payload: GetMemoryProfileInput) -> dict[str, Any]:
        if self.manager.memory is None:
            raise RuntimeError("Memory profiles are not enabled.")
        profile = await self.manager.memory.get(payload.profile_name)
        if profile is None:
            raise KeyError(f"Memory profile not found: {payload.profile_name!r}")
        return profile.model_dump()

    async def _list_memory_profiles(self, _: EmptyInput) -> list[dict[str, Any]]:
        if self.manager.memory is None:
            return []
        return await self.manager.memory.list()

    async def _delete_memory_profile(self, payload: DeleteMemoryProfileInput) -> dict[str, Any]:
        if self.manager.memory is None:
            raise RuntimeError("Memory profiles are not enabled.")
        deleted = await self.manager.memory.delete(payload.profile_name)
        return {"name": payload.profile_name, "deleted": deleted}

    async def _get_session(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.get_session_record(payload.session_id)

    async def _observe(self, payload: ObserveInput) -> dict[str, Any]:
        return await self.manager.observe(payload.session_id, limit=payload.limit, preset=payload.preset)

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

    async def _resume_agent_job(self, payload: ResumeAgentJobInput) -> dict[str, Any]:
        return await self.job_queue.resume_job(payload.job_id, max_steps=payload.max_steps)

    async def _discard_agent_job(self, payload: AgentJobIdInput) -> dict[str, Any]:
        return await self.job_queue.discard_job(payload.job_id)

    async def _cancel_agent_job(self, payload: AgentJobIdInput) -> dict[str, Any]:
        return await self.job_queue.cancel_job(payload.job_id)

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

    async def _readiness_check(self, payload: ReadinessCheckInput) -> dict[str, Any]:
        report = run_readiness_checks(self.manager.settings, mode=payload.mode)
        return report.to_dict()

    # ── Extended tool handlers ──────────────────────────────────────────────

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
        if payload.storage_type not in {"local", "session"}:
            raise ValueError(f"Invalid storage_type: {payload.storage_type!r}")
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
        if payload.storage_type not in {"local", "session"}:
            raise ValueError(f"Invalid storage_type: {payload.storage_type!r}")
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
            provider=payload.provider,
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
