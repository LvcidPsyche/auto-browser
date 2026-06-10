from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel

from ..action_errors import BrowserActionError
from ..approvals import ApprovalRequiredError
from ..models import (
    BrowserActionDecision,
    McpToolCallContent,
    McpToolCallRequest,
    McpToolCallResponse,
)
from ..readiness import run_readiness_checks
from ..tool_inputs import (
    AgentJobIdInput,
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
    ForkSessionInput,
    GetCookiesInput,
    GetMemoryProfileInput,
    GetNetworkLogInput,
    GetPageHtmlInput,
    GetRemoteAccessInput,
    GetStorageInput,
    HarnessGetStatusInput,
    HarnessGetTraceInput,
    HarnessGraduateInput,
    HarnessListRunsInput,
    HarnessSkillIdInput,
    HarnessStartConvergenceInput,
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
    VerifyWitnessInput,
    VisionFindInput,
    WaitForSelectorInput,
)
from .packs import register_all
from .registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)


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
        harness_service=None,
        metrics=None,
    ):
        self.manager = manager
        self.orchestrator = orchestrator
        self.job_queue = job_queue
        self.tool_profile = "full" if tool_profile == "full" else "curated"
        self.cron_service = cron_service
        self.share_manager = share_manager
        self.proxy_store = proxy_store
        self.vision_targeter = vision_targeter
        self.harness_service = harness_service
        self.metrics = metrics
        self._registry = ToolRegistry(
            tool_profile=self.tool_profile,
            experimental_enabled=self._experimental_enabled,
        )
        register_all(self._registry, self)
        if self.vision_targeter is None:
            self._registry.unregister("browser.find_by_vision")
        self._tools = self._registry.tools

    def _experimental_enabled(self, name: str | None) -> bool:
        return name is None

    def list_tools(self) -> list[dict[str, Any]]:
        return self._registry.list_tools()

    async def call_tool(self, payload: McpToolCallRequest) -> McpToolCallResponse:
        started = time.perf_counter()
        metric_tool = payload.name if self._registry.get(payload.name) is not None else "__unknown__"
        status = "error"
        try:
            response = await self._call_tool(payload)
            status = "error" if response.isError else "ok"
            duration_seconds = time.perf_counter() - started
            response.meta = self._tool_response_meta(
                existing=response.meta,
                tool=metric_tool,
                status=status,
                duration_seconds=duration_seconds,
            )
            return response
        finally:
            if self.metrics is not None:
                duration_seconds = time.perf_counter() - started
                try:
                    self.metrics.record_mcp_tool_call(
                        tool=metric_tool,
                        status=status,
                        duration_seconds=duration_seconds,
                    )
                except Exception:
                    logger.warning("failed to record MCP tool metrics for %s", metric_tool, exc_info=True)

    async def _call_tool(self, payload: McpToolCallRequest) -> McpToolCallResponse:
        spec = self._registry.get(payload.name)
        if spec is None:
            return self._error_response(f"Unknown tool: {payload.name}")

        try:
            raw_arguments = dict(payload.arguments or {})
            policy_profile = self._pop_policy_profile(spec, raw_arguments)
            policy_approval_id = self._pop_policy_approval_id(spec, raw_arguments)
            if spec.name == "browser.eval_js" and policy_profile != "governed":
                return self._error_response("browser.eval_js requires workflow_profile=governed")
            if (
                spec.name == "harness.start_convergence"
                and raw_arguments.get("session_id")
                and raw_arguments.get("mock_final_observation") is None
                and policy_profile != "governed"
            ):
                return self._error_response(
                    "harness.start_convergence with a live session requires workflow_profile=governed"
                )
            if spec.name.startswith("harness.") and self.harness_service is None:
                return self._error_response(
                    "harness service unavailable - check controller startup logs and HARNESS_* config"
                )
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
            logger.exception("tool %s failed", payload.name)
            return self._error_response("Tool execution failed")

    @staticmethod
    def _error_response(message: str) -> McpToolCallResponse:
        return McpToolCallResponse(
            content=[McpToolCallContent(text=message)],
            structuredContent={"error": message},
            isError=True,
        )

    @staticmethod
    def _tool_response_meta(
        *,
        existing: dict[str, Any] | None,
        tool: str,
        status: str,
        duration_seconds: float,
    ) -> dict[str, Any]:
        meta = dict(existing or {})
        meta.setdefault("tool", tool)
        meta.setdefault("status", status)
        meta.setdefault("latency_ms", round(duration_seconds * 1000, 2))
        return meta

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

    def _get_harness_service(self):
        if self.harness_service is not None:
            return self.harness_service
        raise RuntimeError("Harness service is not initialized")

    async def _harness_start_convergence(self, payload: HarnessStartConvergenceInput) -> dict[str, Any]:
        service = self._get_harness_service()
        use_live_session = payload.session_id is not None and payload.mock_final_observation is None
        record = await service.start_convergence(
            payload.contract,
            mock_final_observation=payload.mock_final_observation,
            orchestrator=self.orchestrator if use_live_session else None,
            session_id=payload.session_id,
            provider=payload.provider,
            max_attempts=payload.max_attempts,
        )
        return record.model_dump(mode="json")

    async def _harness_get_status(self, payload: HarnessGetStatusInput) -> dict[str, Any]:
        return self._get_harness_service().get_status(payload.run_id)

    async def _harness_get_trace(self, payload: HarnessGetTraceInput) -> dict[str, Any]:
        return self._get_harness_service().get_trace(payload.run_id, attempt_index=payload.attempt_index)

    async def _harness_list_runs(self, payload: HarnessListRunsInput) -> list[dict[str, Any]]:
        return self._get_harness_service().list_runs(status=payload.status, limit=payload.limit)

    async def _harness_list_candidates(self, _: EmptyInput) -> list[dict[str, Any]]:
        return self._get_harness_service().list_candidates()

    async def _harness_get_candidate(self, payload: HarnessSkillIdInput) -> dict[str, Any]:
        return self._get_harness_service().get_candidate(payload.skill_id)

    async def _harness_check_drift(self, payload: HarnessSkillIdInput) -> dict[str, Any]:
        return await self._get_harness_service().check_drift(payload.skill_id)

    async def _harness_check_all_drifts(self, _: EmptyInput) -> list[dict[str, Any]]:
        return await self._get_harness_service().check_all_drifts()

    async def _harness_graduate(self, payload: HarnessGraduateInput) -> dict[str, Any]:
        return self._get_harness_service().graduate(payload.run_id)

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

    async def _verify_witness(self, payload: VerifyWitnessInput) -> dict[str, Any]:
        return await self.manager.verify_witness_chain(payload.session_id)

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
