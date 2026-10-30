from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, get_args

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, ValidationError

from ..action_errors import BrowserActionError, SessionNotFoundError
from ..approval_display import shown_code_points
from ..approvals import CONTROLLER_SCOPE, ApprovalRequiredError, given_back_notes, held_for_execution
from ..browser.element_ids import refuse_selector_hint
from ..browser.page_read import MOVED_DETAIL, bounded, locked_read, start_read
from ..browser.services.diagnostics import checked_screenshot
from ..browser_scripts import PAGE_TEXT_SCRIPT
from ..models import (
    ActionName,
    ApprovalOrigin,
    BrowserActionDecision,
    McpImageContent,
    McpToolCallContent,
    McpToolCallRequest,
    McpToolCallResponse,
)
from ..readiness import run_readiness_checks
from ..result_shaping import inline_screenshot_path, shape_mcp_result
from ..tool_inputs import (
    EVAL_JS_DEFAULT_TIMEOUT_MS,
    AgentJobIdInput,
    ApprovalDecisionInput,
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
    ExecuteApprovalInput,
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
    ReadDownloadInput,
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
from .drag import drag_drop
from .packs import register_all
from .previews import preview_arguments
from .registry import ToolRegistry, ToolSpec
from .selection import withheld_tool_message

logger = logging.getLogger(__name__)

# Bound on the in-page text walk for find_elements' query mode — a
# catastrophically backtracking regex would otherwise hang page.evaluate
# (and the session) indefinitely.
FIND_ELEMENTS_QUERY_TIMEOUT_SECONDS = 10.0

# How long past its timeout a wait whose page moved is refused, matched or not: an unmatched wait's
# answer comes after Playwright's own timer and an IPC round trip, which fall inside the pad.
MOVED_PAD_SECONDS = 0.25

# Tools allowed to create a session on demand when session_id is omitted and no
# session is live — the first call an agent makes is almost always one of these.
# Everything else resolves an omitted session_id only when exactly one session
# is live; creating a session as a side effect of, say, close_tab helps nobody.
# In the order the no_session error suggests them when create_session is not served.
SESSION_STARTER_TOOLS = (
    "browser.observe",
    "browser.execute_action",
    "browser.get_html",
    "browser.find_elements",
    "browser.screenshot",
    "browser.wait_for_selector",
)
IMPLICIT_SESSION_CREATE_TOOLS = frozenset(SESSION_STARTER_TOOLS)
# Tools that queue an agent step or run: its request carries its own workflow_profile (_call_tool).
QUEUE_TOOLS = frozenset({"browser.queue_agent_run", "browser.queue_agent_step"})


# Characters of a governed tool call's arguments shown to the approving operator.
_APPROVAL_PREVIEW_CHARS = 600
# One character of a json.dumps(ensure_ascii=True) text: past the BMP as its surrogate pair of escapes, as
# another escape (\u202e, \n, \\), or as itself. The preview is cut between two of them.
_JSON_CHARACTER = re.compile(r"\\ud[89ab][0-9a-f]{2}\\u[0-9a-f]{4}|\\u[0-9a-f]{4}|\\.|.", re.DOTALL)


# A viewport PNG is typically well under 1 MB. Anything past this is not worth
# putting in a model's context; the text result still carries its URL.
_INLINE_IMAGE_MAX_BYTES = 4 * 1024 * 1024
_INLINE_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _page_of(content: str, *, offset: int, max_chars: int) -> dict[str, Any]:
    """One page of a long text result, and where the next one starts."""
    end = offset + max_chars
    truncated = end < len(content)
    return {
        "content": content[offset:end],
        "offset": offset,
        "total_chars": len(content),
        "truncated": truncated,
        "next_offset": end if truncated else None,
    }


def _cut_between_characters(dumped: str, limit: int) -> str:
    """The longest start of a json.dumps text within ``limit`` characters that splits none of its escapes.

    Cut inside one, the preview ended in \\u202 and did not say that a bidi
    override came next. Read from the start: an escaped backslash before plain
    u202e reads as an escape from its end.
    """
    end = 0
    for character in _JSON_CHARACTER.finditer(dumped):
        if character.end() > limit:
            break
        end = character.end()
    return dumped[:end]


def _read_inline_image(path: Path) -> bytes | None:
    if path.stat().st_size > _INLINE_IMAGE_MAX_BYTES:
        return None
    return path.read_bytes()


_ACTION_NAMES = frozenset(get_args(ActionName))


def _error_text(err: dict[str, Any]) -> str:
    # Pydantic prefixes a validator's own message with "Value error, ", which
    # reads as noise to the agent that has to fix the call.
    return str(err["msg"]).removeprefix("Value error, ")


class McpToolGateway:
    def __init__(
        self,
        *,
        manager,
        orchestrator,
        job_queue,
        tool_profile: str = "curated",
        tool_packs: str = "",
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
        self.cron_service = cron_service
        self.share_manager = share_manager
        self.proxy_store = proxy_store
        self.vision_targeter = vision_targeter
        self.harness_service = harness_service
        self.metrics = metrics
        self._registry = ToolRegistry(
            tool_profile=tool_profile,
            tool_packs=tool_packs,
            experimental_enabled=self._experimental_enabled,
        )
        self.tool_selection = self._registry.selection
        self.tool_profile = self.tool_selection.profile
        register_all(self._registry, self)
        if self.vision_targeter is None:
            self._registry.unregister("browser.find_by_vision")
            if "vision" in self.tool_selection.requested and "vision" in self.tool_selection.packs:
                logger.warning(
                    "MCP_TOOL_PACKS adds vision, but no vision model is configured (set ANTHROPIC_API_KEY): "
                    "browser.find_by_vision is not listed"
                )
        self._tools = self._registry.tools
        logger.info("MCP tools: %s, %d tools listed", self.tool_selection.describe(), len(self._tools))

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
            pack = self._registry.withheld_pack(payload.name)
            if pack is not None:
                return self._error_response(withheld_tool_message(payload.name, pack, self.tool_selection, self._tools))
            return self._error_response(self._unknown_tool_message(payload.name))

        try:
            raw_arguments = dict(payload.arguments or {})
            policy_profile = self._pop_policy_profile(spec, raw_arguments)
            policy_approval_id = self._pop_policy_approval_id(spec, raw_arguments)
            if spec.name == "browser.eval_js":
                # Arbitrary JavaScript in an authenticated page is never ungoverned.
                # This used to be an error unless the caller passed
                # workflow_profile=governed — a parameter the tool's schema never
                # advertised — so the tool in the default set could only fail.
                # The server now routes every call through approval itself.
                policy_profile = "governed"
            if spec.name == "browser.create_cron_job":
                # The job's runs keep what the creating caller was held to. Read before
                # the force override below: force decides at call time (the orchestrator
                # applies it to every run), and is not stored on the job.
                raw_arguments["workflow_profile"] = "governed" if policy_profile == "governed" else "fast"
            if (
                spec.name in QUEUE_TOOLS
                and policy_profile == "governed"
                and isinstance(raw_arguments.get("request"), dict)
            ):
                # As for a cron job: a governed caller's queued run or step runs governed, whatever its request
                # names. A fast caller's request keeps its own profile.
                raw_arguments["request"] = {**raw_arguments["request"], "workflow_profile": "governed"}
            if spec.governed_kind is not None and self.manager.settings.force_governed_approvals:
                # workflow_profile is the caller's argument, and the caller is
                # the agent the approval is meant to check: on a deployment that
                # opted in, the server decides.
                policy_profile = "governed"
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
            arguments = await self._resolve_implicit_session(spec, arguments)
            # Before the handler, for tools that read a session's records
            # without resolving the session itself (witness, audit, downloads).
            session_id = getattr(arguments, "session_id", None)
            # Controller-wide approvals are listed by their owner: their scope names no session.
            listing_controller = spec.name == "browser.list_approvals" and session_id == CONTROLLER_SCOPE
            if isinstance(session_id, str) and session_id and not listing_controller:
                await self.manager.ensure_session_accessible(session_id)
            approval = await self._require_governed_tool_approval(
                spec,
                arguments,
                workflow_profile=policy_profile,
                approval_id=policy_approval_id,
            )
            if approval is None:
                result = await spec.handler(arguments)
            else:
                # Only execute_action's block is a single action call; any other
                # handler may change the page itself, so its approval is never given back.
                async with held_for_execution(
                    self.manager.approvals, approval.id, may_give_back=spec.name == "browser.execute_action"
                ):
                    result = await spec.handler(arguments)
            result = shape_mcp_result(spec.name, result, detail=getattr(arguments, "detail", "compact"))
            # The JSON stays the first block: clients (and the LangChain
            # adapter) read content[0].text as the result.
            content: list[McpToolCallContent | McpImageContent] = [
                McpToolCallContent(text=json.dumps(result, ensure_ascii=False))
            ]
            image = await self._inline_image(spec.name, result)
            if image is not None:
                content.append(image)
            return McpToolCallResponse(content=content, structuredContent=result, isError=False)
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
        except ValidationError as exc:
            # Invalid tool arguments — report the field errors so the calling
            # agent can fix its call, instead of "Tool execution failed".
            details = "; ".join(
                f"{'.'.join(str(part) for part in err['loc'])}: {_error_text(err)}"
                if err.get("loc")
                else _error_text(err)
                for err in exc.errors()
            )
            return self._error_response(f"Invalid arguments for {payload.name}: {details}")
        except SessionNotFoundError as exc:
            return self._refusal_response(
                exc.message,
                {
                    "error": exc.message,
                    "code": exc.code,
                    **({"reason": exc.reason} if exc.reason else {}),
                    "session_id": exc.session_id,
                },
                exc,
            )
        except (ValueError, KeyError, RuntimeError) as exc:
            # Handlers raise these with operator-facing messages ("Provide
            # source_selector or source_x/source_y", "Memory profile not found").
            # Surface them so the calling agent can self-correct, instead of
            # collapsing them into the opaque catch-all below.
            message = str(exc.args[0]) if exc.args else exc.__class__.__name__
            return self._refusal_response(message, {"error": message}, exc)
        except FileNotFoundError as exc:
            # Lookups that miss raise FileNotFoundError with a message naming
            # what the caller asked for: an auth profile, an upload file. A
            # mistyped profile name reached agents as "Tool execution failed".
            # One raised by the OS carries an errno and a server path, so it
            # stays opaque, as with PermissionError below.
            if exc.errno is not None:
                logger.exception("tool %s failed", payload.name)
                return self._refusal_response("Tool execution failed", {"error": "Tool execution failed"}, exc)
            message = str(exc.args[0]) if exc.args else "Not found"
            return self._refusal_response(message, {"error": message, "code": "not_found"}, exc)
        except PermissionError as exc:
            # Policy refusals raise PermissionError with a reason for the caller:
            # a host outside ALLOWED_HOSTS, an approval not yet granted or not
            # matching the action, a path outside its root. They reached agents
            # as "Tool execution failed", so a blocked navigation and a retry
            # before the operator approved looked like crashes. An OS-level
            # PermissionError carries an errno and can name a server path; that
            # one stays opaque.
            if exc.errno is not None:
                logger.exception("tool %s failed", payload.name)
                return self._refusal_response("Tool execution failed", {"error": "Tool execution failed"}, exc)
            message = str(exc.args[0]) if exc.args else "Not permitted"
            return self._refusal_response(message, {"error": message, "code": "not_permitted"}, exc)
        except PlaywrightError as exc:
            # The browser's answer about the caller's own page: a selector that
            # never appeared, an invalid selector, a script that threw. These
            # reached agents as "Tool execution failed", so a timeout looked
            # like a crash and an eval_js error could not be debugged.
            # Keep the reason; drop Playwright's call log and the stack frames of
            # its own evaluation wrapper, which say nothing about the page.
            message = (getattr(exc, "message", None) or str(exc)).split("\nCall log:")[0]
            message = message.split("\n    at ")[0].strip()
            code = "timeout" if isinstance(exc, PlaywrightTimeoutError) else "browser_error"
            return self._refusal_response(message[:2000], {"error": message[:2000], "code": code}, exc)
        except Exception as exc:
            logger.exception("tool %s failed", payload.name)
            return self._refusal_response("Tool execution failed", {"error": "Tool execution failed"}, exc)

    async def _inline_image(self, tool_name: str, result: Any) -> McpImageContent | None:
        """The result's screenshot as MCP image content, when the tool's job is to show the page."""
        path_value = inline_screenshot_path(tool_name, result)
        if path_value is None:
            return None
        artifact_root = getattr(getattr(self.manager, "settings", None), "artifact_root", None)
        if not artifact_root:
            return None
        path = Path(path_value).resolve()
        # The path comes from the manager, but it is read back and sent to the
        # client, so it has to be a screenshot artifact and nothing else.
        if not path.is_relative_to(Path(artifact_root).resolve()):
            logger.warning("not inlining %s: outside the artifact root", path)
            return None
        mime_type = _INLINE_IMAGE_TYPES.get(path.suffix.lower())
        if mime_type is None:
            return None
        try:
            data = await asyncio.to_thread(_read_inline_image, path)
        except OSError as exc:
            logger.debug("could not inline screenshot %s: %s", path, exc)
            return None
        if data is None:
            return None
        return McpImageContent(data=base64.b64encode(data).decode("ascii"), mimeType=mime_type)

    def _unknown_tool_message(self, name: str) -> str:
        """Point a mistyped or guessed tool name at the tool the caller meant."""
        message = f"Unknown tool: {name}"
        served = list(self._registry.tools)
        # A client that rewrote the dot, or dropped the namespace.
        for candidate in (name.replace("_", ".", 1), f"browser.{name}"):
            if candidate in served:
                return f"{message}. Did you mean {candidate}?"
        # A browser action guessed as its own tool (browser.click, navigate, ...).
        action = name.rsplit(".", 1)[-1]
        if action in _ACTION_NAMES and "browser.execute_action" in served:
            return f"{message}. Actions go through browser.execute_action: action={{'action': '{action}', ...}}."
        close = difflib.get_close_matches(name, served, n=1, cutoff=0.8)
        if close:
            return f"{message}. Did you mean {close[0]}?"
        return message

    @staticmethod
    def _refusal_response(message: str, structured: dict[str, Any], exc: BaseException) -> McpToolCallResponse:
        """An error answer that also says what became of each approval given back with ``exc``.

        Only a BrowserActionError or an approval_required answer carried that,
        so an agent refused by a plain PermissionError (an inner approval that is
        not approved) was not told its governed approval was still approved.
        """
        notes = given_back_notes(exc)
        if notes:
            structured = {**structured, **notes}
            message = f"{message} ({notes['approval_given_back']['message']})"
        return McpToolCallResponse(
            content=[McpToolCallContent(text=message)], structuredContent=structured, isError=True
        )

    @staticmethod
    def _error_response(message: str) -> McpToolCallResponse:
        return McpToolCallResponse(
            content=[McpToolCallContent(text=message)],
            structuredContent={"error": message},
            isError=True,
        )

    async def _resolve_implicit_session(self, spec: Any, arguments: BaseModel) -> BaseModel:
        """Resolve an omitted session_id against the live session set.

        Exactly one live session → target it. None live → create one on demand,
        but only for observe/act tools (IMPLICIT_SESSION_CREATE_TOOLS). Anything
        ambiguous stays an explicit, actionable error rather than a guess.
        """
        if not isinstance(arguments, SessionIdInput) or arguments.session_id:
            return arguments
        # list_sessions() also returns persisted records of closed and
        # interrupted sessions. Counting those meant that after one session was
        # closed, an omitted session_id either targeted the closed session or
        # was reported "ambiguous" — with MAX_SESSIONS=1, every create → close →
        # create cycle broke the convenience for good. Only a live session can
        # be acted on, so only live sessions are candidates.
        sessions = [item for item in await self.manager.list_sessions() if item.get("live") is True]
        if len(sessions) == 1:
            return arguments.model_copy(update={"session_id": sessions[0]["id"]})
        if not sessions:
            if spec.name in IMPLICIT_SESSION_CREATE_TOOLS:
                created = await self.manager.create_session()
                return arguments.model_copy(update={"session_id": created["id"]})
            raise BrowserActionError(
                f"{spec.name} needs a session and none are live{self._no_session_hint()}",
                code="no_session",
                action=spec.name,
            )
        ids = ", ".join(str(item.get("id")) for item in sessions)
        raise BrowserActionError(
            f"session_id is required when multiple sessions are live ({ids}).",
            code="ambiguous_session",
            action=spec.name,
        )

    def _no_session_hint(self) -> str:
        """How to get a session, naming only tools this controller serves (lean has no create_session)."""
        starter = next((name for name in SESSION_STARTER_TOOLS if name in self._tools), None)
        if "browser.create_session" in self._tools:
            if starter is None:
                return " — create one with browser.create_session."
            return (
                " — create one with browser.create_session (or call an observe/act tool, which creates one on demand)."
            )
        if starter is None:
            return "."
        return f" — call {starter}, which creates one on demand."

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
        # No session_id: the approval goes in the controller-wide scope, so a
        # tool that names no session (delete_memory_profile, cdp_attach) is
        # governed as well; require_governed_approval takes None for that.
        session_id = getattr(arguments, "session_id", None) or None
        decision = getattr(arguments, "action", None)
        reason = None
        whole_arguments = None
        origin: ApprovalOrigin = "action"
        if not isinstance(decision, BrowserActionDecision):
            if approval_id:
                # Spending the approval it names: matched on the stand-in's text alone, so neither the
                # preview nor the kept arguments are built. Its digest dumps and hashes them all the
                # same, so off the event loop too.
                decision, reason = await asyncio.to_thread(self._governed_call_decision, spec, arguments)
            else:
                # Off the event loop, which serves every session: megabytes of arguments take seconds to
                # write out, and the preview and the digest dump and hash them whole again.
                def asked() -> tuple[str, tuple[BrowserActionDecision, str]]:
                    whole = self._governed_call_arguments(spec, arguments)
                    return whole, self._governed_call_decision(spec, arguments, arguments_size=len(whole))

                whole_arguments, (decision, reason) = await asyncio.to_thread(asked)
            # Only here, where the stand-in is built: the same fields from a
            # caller are an action approval, which no tool call can spend.
            origin = "tool_call"
        elif decision.action == "upload":
            # Before the operator is asked, and before the approval is claimed:
            # a missing file would spend it on not_found. execute_decision checks
            # in this order for the runtime approval.
            session = await self.manager.get_session(session_id) if session_id else None
            await self.manager.uploads.safe_path(decision.file_path or "", session=session)
        return await self.manager.require_governed_approval(
            session_id,
            decision,
            approval_id=approval_id,
            reason=reason,
            origin=origin,
            arguments=whole_arguments,
        )

    @staticmethod
    def _governed_call_arguments(spec: ToolSpec, arguments: BaseModel) -> str:
        """A governed call's whole arguments as the operator may view them: the preview's, uncut and indented.

        Credentials left out as in the preview (preview_arguments), and every
        character past ASCII written as its escape, so the text holds nothing
        hidden but the newlines of its indent.
        """
        args = arguments.model_dump(mode="json", exclude={"session_id", "approval_id"}, exclude_none=True)
        return json.dumps(preview_arguments(spec.name, args), sort_keys=True, indent=2, ensure_ascii=True)

    @staticmethod
    def _governed_call_decision(
        spec: ToolSpec, arguments: BaseModel, *, arguments_size: int | None = None
    ) -> tuple[BrowserActionDecision, str]:
        """Stand-in decision for approving a governed tool call that is not a browser action.

        An approval has to be for *this* call. The stand-in used to carry only the
        tool name, and approval matching ignores the reason, so one approval
        authorised any arguments: an operator approving browser.eval_js approved
        code they never saw, and it covered whatever expression came next. The
        canonical arguments are now hashed into ``text``, which matching compares,
        and previewed in the reason the operator reads. Credential-bearing fields
        are left out of the preview (approvals are stored and sent to webhooks);
        the hash still covers them.

        ``arguments_size`` is the length of the whole text View all shows
        (_governed_call_arguments), which a cut preview names: its own one-line
        form is shorter, and counting that gave one call two totals. Without it
        the reason names only the tool, for a caller that needs only ``text``.
        """
        args = arguments.model_dump(mode="json", exclude={"session_id", "approval_id"}, exclude_none=True)
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        reason = f"Approve {spec.name}"
        if arguments_size is not None:
            # Every character past ASCII as its \uXXXX escape: a homoglyph host, an invisible name or a
            # line separator that ends a // comment reads as itself, not as the character it imitates.
            # Cut before it is escaped: what is shown is all that needs escaping, and escaping
            # megabytes first blocked the event loop for tens of seconds.
            dumped = json.dumps(preview_arguments(spec.name, args), sort_keys=True, ensure_ascii=True)
            preview = shown_code_points(_cut_between_characters(dumped, _APPROVAL_PREVIEW_CHARS))
            if len(dumped) > _APPROVAL_PREVIEW_CHARS:
                preview = f"{preview}… ({arguments_size} characters in all: View all shows them)"
            reason = f"{reason} with {preview}"
        decision = BrowserActionDecision(
            action="request_human_takeover",
            reason=reason[:1000],
            risk_category=spec.governed_kind if spec.governed_kind != "dynamic" else "write",
            text=f"{spec.name} sha256:{digest}",
        )
        return decision, reason

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
            totp_hosts=payload.totp_hosts,
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

    async def _read_download(self, payload: ReadDownloadInput) -> dict[str, Any]:
        download = await self.manager.read_download_text(payload.session_id, payload.download_id)
        text = download.pop("text")
        return {
            "session_id": payload.session_id,
            **download,
            **_page_of(text, offset=payload.offset, max_chars=payload.max_chars),
        }

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
        # The caller here is the agent whose action is waiting: approving it
        # would make the approval meaningless unless the deployment opted in.
        if not self.manager.settings.autonomous_approvals:
            raise PermissionError(
                "approvals come from an operator, in the dashboard or with POST /approvals/{id}/approve; "
                "set AUTONOMOUS_APPROVALS=true on the controller to let agents approve their own actions"
            )
        return await self.manager.approve(payload.approval_id, comment=payload.comment, decided_via="agent")

    async def _reject_approval(self, payload: ApprovalDecisionInput) -> dict[str, Any]:
        return await self.manager.reject(payload.approval_id, comment=payload.comment, decided_via="agent")

    async def _execute_approval(self, payload: ExecuteApprovalInput) -> dict[str, Any]:
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

    async def _export_witness_bundle(self, payload: VerifyWitnessInput) -> dict[str, Any]:
        return await self.manager.export_witness_bundle(payload.session_id)

    async def _fork_session(self, payload: ForkSessionInput) -> dict[str, Any]:
        return await self.manager.fork_session(
            payload.session_id,
            name=payload.name,
            start_url=payload.start_url,
        )

    async def _eval_js(self, payload: EvalJsInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        # Each page tool reads under the session lock, checked before and after (locked_read).
        timeout = (payload.timeout_ms or EVAL_JS_DEFAULT_TIMEOUT_MS) / 1000
        async with locked_read(self.manager, session):
            try:
                result = await asyncio.wait_for(session.page.evaluate(payload.expression), timeout=timeout)
            except TimeoutError:
                # Giving up does not stop the page's script: the renderer keeps running it.
                raise RuntimeError(
                    f"The expression did not finish within {timeout:g} seconds; it may still be running in the page."
                ) from None
        return {"session_id": payload.session_id, "result": result}

    async def _wait_for_selector(self, payload: WaitForSelectorInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        refuse_selector_hint(payload.selector, action="wait_for_selector")
        # Not under the session lock: the wait lasts up to its timeout, and holding the lock stalled
        # every action on the session meanwhile, the click that makes the element appear among them.
        # The page is captured before the wait and checked after it, so one that moved is refused.
        read = start_read(self.manager, session)
        deadline = time.monotonic() + payload.timeout_ms / 1000
        try:
            # Bounded by its own timeout_ms.
            await read.page.wait_for_selector(
                payload.selector,
                timeout=payload.timeout_ms,
                state=payload.state,
            )
        except Exception:
            # Judged here, refused below: waiting in this block kept Playwright's error, whose call log
            # can quote the refused page's markup, as the context of the refusal and of a cancel.
            if not read.moved():
                raise
        else:
            if not read.moved():
                return {"session_id": payload.session_id, "selector": payload.selector, "state": payload.state}
        await _refuse_moved_at(deadline + MOVED_PAD_SECONDS)

    async def _get_html(self, payload: GetPageHtmlInput) -> dict[str, Any]:
        # Bounded and paged: a page's serialized DOM is routinely megabytes, and
        # returned whole it overran the context of the model that asked for it.
        session = await self.manager.get_session(payload.session_id)
        async with locked_read(self.manager, session):
            if payload.text_only:
                content = await bounded(session.page.evaluate(PAGE_TEXT_SCRIPT))
                kind = "text"
            else:
                content = await bounded(session.page.content())
                kind = "html"
        return {
            "session_id": payload.session_id,
            "type": kind,
            **_page_of(content, offset=payload.offset, max_chars=payload.max_chars),
        }

    async def _find_elements(self, payload: FindElementsInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        if payload.query is None:
            refuse_selector_hint(payload.selector, action="find_elements")
        async with locked_read(self.manager, session):
            return await self._find_elements_read(session, payload)

    async def _find_elements_read(self, session: Any, payload: FindElementsInput) -> dict[str, Any]:
        if payload.query is not None:
            evaluate = session.page.evaluate(
                """([query, isRegex, context, limit]) => {
                    let matcher = null;
                    if (isRegex) {
                        try {
                            matcher = new RegExp(query, 'gi');
                        } catch (e) {
                            return {__invalid_regex: String((e && e.message) || e)};
                        }
                    }
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    const results = [];
                    let node;
                    while ((node = walker.nextNode()) && results.length < limit) {
                        const text = node.textContent || '';
                        if (!text.trim()) continue;
                        let matchIndex = -1;
                        let matchText = '';
                        if (isRegex) {
                            matcher.lastIndex = 0;
                            const m = matcher.exec(text);
                            if (m) { matchIndex = m.index; matchText = m[0]; }
                        } else {
                            const idx = text.toLowerCase().indexOf(query.toLowerCase());
                            if (idx !== -1) { matchIndex = idx; matchText = text.substr(idx, query.length); }
                        }
                        if (matchIndex === -1) continue;
                        const el = node.parentElement;
                        if (!el) continue;
                        const r = el.getBoundingClientRect();
                        const start = Math.max(0, matchIndex - context);
                        const end = Math.min(text.length, matchIndex + matchText.length + context);
                        results.push({
                            tag: el.tagName.toLowerCase(),
                            text: text.trim().substring(0, 200),
                            match: matchText,
                            context_text: text.substring(start, end).trim(),
                            value: el.value || null,
                            href: el.href || null,
                            id: el.id || null,
                            class: (typeof el.className === 'string' ? el.className : el.getAttribute('class')) || null,
                            visible: r.width > 0 && r.height > 0,
                            x: Math.round(r.x), y: Math.round(r.y),
                            width: Math.round(r.width), height: Math.round(r.height),
                        });
                    }
                    return results;
                }""",
                [payload.query, payload.regex, payload.context, payload.limit],
            )
            try:
                elements = await asyncio.wait_for(evaluate, timeout=FIND_ELEMENTS_QUERY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise BrowserActionError(
                    f"find_elements query timed out after "
                    f"{FIND_ELEMENTS_QUERY_TIMEOUT_SECONDS:g}s — the pattern may "
                    "backtrack catastrophically on this page's text",
                    code="query_timeout",
                    action="find_elements",
                ) from None
            if isinstance(elements, dict) and "__invalid_regex" in elements:
                raise BrowserActionError(
                    f"Invalid regular expression: {elements['__invalid_regex']}",
                    code="invalid_regex",
                    action="find_elements",
                )
            return {"session_id": payload.session_id, "query": payload.query, "elements": elements}

        elements = await bounded(
            session.page.evaluate(
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
                        class: (typeof el.className === 'string' ? el.className : el.getAttribute('class')) || null,
                        visible: r.width > 0 && r.height > 0,
                        x: Math.round(r.x), y: Math.round(r.y),
                        width: Math.round(r.width), height: Math.round(r.height),
                    };
                });
            }""",
                [payload.selector, payload.limit],
            )
        )
        return {"session_id": payload.session_id, "selector": payload.selector, "elements": elements}

    async def _drag_drop(self, payload: DragDropInput) -> dict[str, Any]:
        return await drag_drop(self.manager, payload)

    async def _set_viewport(self, payload: SetViewportInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        await session.page.set_viewport_size({"width": payload.width, "height": payload.height})
        return {"session_id": payload.session_id, "width": payload.width, "height": payload.height}

    async def _get_cookies(self, payload: GetCookiesInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        urls = payload.urls
        if urls:
            # Nothing is answered for a URL the list refuses. With none left, nothing at all:
            # cookies(urls=[]) would return every cookie.
            urls = [url for url in urls if not self.manager._url_off_list(url)]
            if not urls:
                return {"session_id": payload.session_id, "cookies": []}
        cookies = await session.context.cookies(urls=urls)
        # A page on a host ALLOWED_HOSTS refuses sets cookies before it is blanked, and the
        # context keeps them: a cookie is returned only when a host the list allows would
        # receive it.
        allowed = [
            c
            for c in cookies
            if not self.manager._cookie_off_list(c["domain"])
            and not self.manager._partition_off_list(c.get("partitionKey"))
        ]
        return {"session_id": payload.session_id, "cookies": allowed}

    async def _set_cookies(self, payload: SetCookiesInput) -> dict[str, Any]:
        session = await self.manager.get_session(payload.session_id)
        # Cookies are scoped to a site, so ALLOWED_HOSTS has to cover the site
        # each one is for: otherwise a caller confined to one host plants a
        # session cookie on another. All are checked before any is added.
        for cookie in payload.cookies:
            url = str(cookie.get("url") or "").strip()
            domain = str(cookie.get("domain") or "").strip()
            if url:
                self.manager._assert_url_allowed(url)
            if domain:
                self.manager._assert_url_allowed(f"https://{domain.lstrip('.')}/")
        await session.context.add_cookies(payload.cookies)
        return {"session_id": payload.session_id, "set": len(payload.cookies)}

    async def _get_local_storage(self, payload: GetStorageInput) -> dict[str, Any]:
        if payload.storage_type not in {"local", "session"}:
            raise ValueError(f"Invalid storage_type: {payload.storage_type!r}")
        session = await self.manager.get_session(payload.session_id)
        if payload.key:
            script = f"() => window.{payload.storage_type}Storage.getItem({payload.key!r})"
            async with locked_read(self.manager, session):
                value = await bounded(session.page.evaluate(script))
            return {"session_id": payload.session_id, "key": payload.key, "value": value}
        script = (
            f"() => Object.fromEntries("
            f"Object.keys(window.{payload.storage_type}Storage).map("
            f"k => [k, window.{payload.storage_type}Storage.getItem(k)]))"
        )
        async with locked_read(self.manager, session):
            data = await bounded(session.page.evaluate(script))
        return {"session_id": payload.session_id, "storage": data}

    async def _set_local_storage(self, payload: SetStorageInput) -> dict[str, Any]:
        if payload.storage_type not in {"local", "session"}:
            raise ValueError(f"Invalid storage_type: {payload.storage_type!r}")
        session = await self.manager.get_session(payload.session_id)
        script = f"([k, v]) => window.{payload.storage_type}Storage.setItem(k, v)"
        # Refused before the write, and after it if the page moved meanwhile: the
        # value may then be in a refused page's storage.
        async with locked_read(self.manager, session):
            await bounded(session.page.evaluate(script, [payload.key, payload.value]))
        return {"session_id": payload.session_id, "key": payload.key, "set": True}

    async def _export_script(self, payload: ExportScriptInput) -> dict[str, Any]:
        from ..playwright_export import export_session_script

        # The record, not the live session: a finished session can be exported.
        await self.manager.get_session_record(payload.session_id)
        return await export_session_script(
            payload.session_id,
            self.manager.audit,
            viewport_w=self.manager.settings.default_viewport_width,
            viewport_h=self.manager.settings.default_viewport_height,
        )

    async def _cdp_attach(self, payload: CdpAttachInput) -> dict[str, Any]:
        return await self.manager.cdp_attach(payload.cdp_url)

    async def _find_by_vision(self, payload: VisionFindInput) -> dict[str, Any]:
        if self.vision_targeter is None:
            raise RuntimeError("Vision targeting is not available — set ANTHROPIC_API_KEY to enable it.")
        session = await self.manager.get_session(payload.session_id)
        if payload.take_screenshot:
            screenshot = await self.manager.capture_screenshot(payload.session_id, label="vision")
            screenshot_path = screenshot["screenshot_path"]
        else:
            # The last screenshot a checked read returned, not the newest PNG on disk.
            screenshot = checked_screenshot(session)
            if screenshot is None:
                raise RuntimeError("No screenshots available — take one first")
            screenshot_path = screenshot["path"]

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
            workflow_profile=payload.workflow_profile,
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


async def _refuse_moved_at(deadline: float) -> None:
    """Refuse a wait whose page moved at ``deadline`` (time.monotonic()): its timeout plus MOVED_PAD_SECONDS.

    Refused at once, the answer came right after the selector matched on the refused document and at the
    timeout when it did not, so its latency told whether it matched there. The session lock is not held
    by the wait, so only its caller waits. Called outside the wait's except block, so neither the
    refusal nor a cancel meanwhile carries the wait's own error, which can quote the page's text.
    """
    while (remaining := deadline - time.monotonic()) > 0:
        await asyncio.sleep(remaining)
    raise PermissionError(MOVED_DETAIL) from None
