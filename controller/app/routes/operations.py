from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..action_errors import BROWSER_UNREACHABLE_DETAIL, BrowserActionError, BrowserUnreachableError
from ..agent_jobs import AgentJobQueueFull
from ..approvals import ServerStateRefusal
from ..auth_state import AuthStateDecryptError
from ..browser.services.auth_profiles import UncheckedAuthProfileError
from ..browser.services.session_creation import SessionLimitError
from ..cron_service import CronJobDisabledError, WebhookKeyRefusedError, job_not_found_text
from ..models import ApprovalDecisionRequest
from ..tool_inputs import CreateCronJobInput, CreateProxyPersonaInput, CronJobIdInput, TriggerCronJobInput
from ..utils import capped_for_log, error_text, quoted_for_log
from ._utils import (
    bounded_limit,
    cron_store_errors,
    log_hidden_cause,
    persona_file_errors,
    queue_full,
    typed_route_errors,
    unknown_id_is_404,
)

logger = logging.getLogger(__name__)

# What a route passes typed_route_errors as its ``refusal`` when the manager's refusal is a state conflict: an
# approval that is not approved, a proxy persona another operator owns.
CONFLICT = (409, "Conflict")


# The cap's own text names the job owner's live session ids (check_limit runs as the owner), and the trigger checks
# only the job's webhook key, which another operator may hold: the trigger names no session (W2-14).
CRON_TRIGGER_SESSION_LIMIT_DETAIL = (
    "Session limit reached: the job's run could not start a session. Close a session or raise MAX_SESSIONS."
)
# The job's auth profile is gone, or is another operator's since the job was made: a read answers both as missing
# (FileNotFoundError), and the trigger had no case for it. One text for both, which names no profile, path or owner.
# And for a profile whose profile.json no longer parses (UncheckedAuthProfileError): nobody may open that one.
CRON_TRIGGER_AUTH_PROFILE_DETAIL = (
    "The job's run could not start: its auth profile does not exist, or the job's owner may not open it. Save the "
    "profile as the job's owner, or delete the job and create it again with another profile."
)
# With the key proved, anything else the run's start refuses: its session (an auth state too old or not encrypted,
# a start_url ALLOWED_HOSTS no longer lists) or the job as it is stored. The caller holds a webhook key, not the
# operator's token, so the text names nothing of the cause: no host, no age, no setting. The log has it.
CRON_TRIGGER_RUN_REFUSED_DETAIL = (
    "The job's run could not start: the controller refused its session, or the job as it is stored. The webhook "
    "key was accepted; the server log names the cause."
)
# With the key proved, a start that may work at the next try: the job's start_url did not load (a name that did not
# resolve, a timeout), or the browser's pre-request gate or window watcher was not attached. A 503, since a webhook's
# sender tries a 5xx again and not a 4xx. It names as little as the text above.
CRON_TRIGGER_TRY_AGAIN_DETAIL = (
    "The job's run could not start this time: its session's start page did not load, or the browser was not ready "
    "for a session. The webhook key was accepted; try again. The server log names the cause."
)


def create_operations_router(
    *,
    manager: Any,
    proxy_store: Any,
    cron_service: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/remote-access")
    async def get_remote_access(session_id: str | None = None) -> dict[str, Any]:
        with typed_route_errors(logger, "remote access info failed"):
            if session_id and session_id not in manager.sessions:
                try:
                    record = await manager.get_session_record(session_id)
                    return record["remote_access"]
                except KeyError:
                    raise HTTPException(status_code=404, detail="Unknown session") from None
            return manager.get_remote_access_info(session_id)

    @router.get("/audit/events")
    async def list_audit_events(
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "audit events listing failed"):
            return await manager.list_audit_events(
                limit=bounded_limit(limit, 500),
                session_id=session_id,
                event_type=event_type,
                operator_id=operator_id,
            )

    @router.get("/approvals")
    async def list_approvals(status: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "approvals listing failed"):
            return await manager.list_approvals(status=status, session_id=session_id)

    @router.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "get approval failed for approval %s", approval_id):
            with unknown_id_is_404(approval_id):
                return await manager.get_approval(approval_id)

    # A tool call's whole arguments, which the approval's reason previews; the record's access rule.
    @router.get("/approvals/{approval_id}/arguments")
    async def get_approval_arguments(approval_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "approval arguments failed for approval %s", approval_id):
            with unknown_id_is_404(approval_id):
                return await manager.approval_arguments(approval_id)

    # A decision refused for what the server knows (an approval its action holds, or one whose
    # arguments or text are gone) answers with why: ServerStateRefusal names nothing an agent
    # wrote. Any other refusal keeps the fixed text, and its cause goes to the log (typed_route_errors).
    @router.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "approve failed for approval %s", approval_id, refusal=CONFLICT):
            try:
                with unknown_id_is_404(approval_id):
                    return await manager.approve(approval_id, comment=payload.comment)
            except ServerStateRefusal as exc:
                raise HTTPException(status_code=409, detail=error_text(exc)) from None

    @router.post("/approvals/{approval_id}/reject")
    async def reject_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "reject failed for approval %s", approval_id, refusal=CONFLICT):
            try:
                with unknown_id_is_404(approval_id):
                    return await manager.reject(approval_id, comment=payload.comment)
            except ServerStateRefusal as exc:
                raise HTTPException(status_code=409, detail=error_text(exc)) from None

    @router.post("/approvals/{approval_id}/execute")
    async def execute_approval(approval_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "approval execution failed for approval %s", approval_id, refusal=CONFLICT):
            with unknown_id_is_404(approval_id):
                return await manager.execute_approval(approval_id)

    @router.get("/pii-scrubber")
    async def get_pii_scrubber() -> dict[str, Any]:
        with typed_route_errors(logger, "pii scrubber status failed"):
            return manager.get_pii_scrubber_status()

    @router.get("/proxy-personas")
    async def list_proxy_personas() -> list[Any]:
        with persona_file_errors():
            return proxy_store.list_personas()

    @router.post("/proxy-personas")
    async def set_proxy_persona(payload: CreateProxyPersonaInput) -> dict[str, Any]:
        # Another operator's persona is the conflict; a ValueError is typed_route_errors' own 400.
        message = "set proxy persona failed for persona %s"
        with typed_route_errors(logger, message, payload.name, refusal=CONFLICT):
            try:
                with persona_file_errors():
                    return proxy_store.set_persona(
                        payload.name,
                        server=payload.server,
                        username=payload.username,
                        password=payload.password,
                        description=payload.description,
                    )
            except RuntimeError as cause:  # no PROXY_PERSONA_FILE to save to
                log_hidden_cause(logger, 400, cause, message, payload.name)
                raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/proxy-personas/{name}")
    async def get_proxy_persona(name: str) -> dict[str, Any]:
        with persona_file_errors():
            try:
                return proxy_store.get_persona(name)
            except KeyError:
                raise HTTPException(status_code=404, detail="Not found") from None

    @router.delete("/proxy-personas/{name}")
    async def delete_proxy_persona(name: str) -> dict[str, Any]:
        with persona_file_errors():
            deleted = proxy_store.delete_persona(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Proxy persona not found: {quoted_for_log(name)}")
        return {"deleted": True, "name": name}

    @router.get("/crons")
    async def list_cron_jobs() -> list[Any]:
        with cron_store_errors():
            return await cron_service.list_jobs()

    @router.post("/crons")
    async def create_cron_job(payload: CreateCronJobInput) -> dict[str, Any]:
        try:
            with cron_store_errors():
                return await cron_service.create_job(
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
                    # REST callers are operators: the profile the body names, fast by default.
                    workflow_profile=payload.workflow_profile,
                )
        except PermissionError as exc:
            # Another operator's auth profile, or one whose owner cannot be checked: a write refuses openly, and only
            # a read answers "missing". One the OS raises carries an errno: the store's fault, not the caller's.
            if exc.errno is not None:
                raise
            log_hidden_cause(logger, 403, exc, "create cron job refused")
            raise HTTPException(status_code=403, detail="Not permitted") from None
        except (ValueError, TypeError) as exc:
            log_hidden_cause(logger, 400, exc, "create cron job failed")
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/crons/{job_id}")
    async def get_cron_job(job_id: str) -> dict[str, Any]:
        with cron_store_errors(), unknown_id_is_404(job_not_found_text(job_id)):
            return await cron_service.get_job(job_id)

    @router.delete("/crons/{job_id}")
    async def delete_cron_job(job_id: str) -> dict[str, Any]:
        with cron_store_errors():
            deleted = await cron_service.delete_job(job_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Cron job not found: {capped_for_log(job_id)}")
        return {"deleted": True, "job_id": job_id}

    @router.post("/crons/{job_id}/webhook-key/rotate")
    async def rotate_cron_webhook_key(job_id: str) -> dict[str, Any]:
        try:
            payload = CronJobIdInput.model_validate({"job_id": job_id})
            with cron_store_errors():
                return await cron_service.rotate_webhook_key(payload.job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found") from None
        except ValidationError as exc:
            log_hidden_cause(logger, 400, exc, "rotate webhook key failed for cron job %s", job_id)
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.delete("/crons/{job_id}/webhook-key")
    async def revoke_cron_webhook_key(job_id: str) -> dict[str, Any]:
        try:
            payload = CronJobIdInput.model_validate({"job_id": job_id})
            with cron_store_errors():
                return await cron_service.revoke_webhook_key(payload.job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found") from None
        except ValidationError as exc:
            log_hidden_cause(logger, 400, exc, "revoke webhook key failed for cron job %s", job_id)
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.post("/crons/{job_id}/trigger")
    async def trigger_cron_job_via_webhook(job_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("trigger body must be a JSON object")
            # The path names the job; a job_id in the body must not redirect the
            # trigger to a different one.
            payload = TriggerCronJobInput.model_validate({**body, "job_id": job_id})
        except (ValidationError, ValueError, RecursionError) as exc:
            # The body's webhook key is never in the line: a model's refusal is logged by field and kind.
            # RecursionError: a body nested past what the JSON reader goes down. It is a RuntimeError, so it went
            # on as a fault of the server's, for a caller who had proved no key.
            log_hidden_cause(logger, 400, exc, "cron trigger failed for job %s", job_id)
            raise HTTPException(status_code=400, detail="Invalid request") from None
        # The body and the path's job id are the caller's, and are judged above: the route's only 400, as the key's
        # refusal below is its only 403. From here on the service reads the cron store and refuses the key before it
        # does anything else: whatever else is raised comes with the key proved, and no mistake of the caller's put
        # it there.
        try:
            # The run's session reads the job's proxy persona.
            with cron_store_errors(), persona_file_errors():
                return await cron_service.trigger_via_webhook(payload.job_id, payload.webhook_key or "")
        except WebhookKeyRefusedError as exc:
            # An unknown id, no key, a wrong one: the only 403 of this route, so that a caller can read it as the key.
            # And a stored key that could not be read, which proves none: the line then names what was raised there.
            cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
            log_hidden_cause(logger, 403, cause, "cron trigger refused for job %s", job_id)
            raise HTTPException(status_code=403, detail="Not permitted") from None
        except UncheckedAuthProfileError:
            # Raised where the run's session opens the job's profile.
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_AUTH_PROFILE_DETAIL) from None
        except PermissionError as exc:
            # A refusal of the run's session: an auth state too old or not encrypted, a start_url ALLOWED_HOSTS no
            # longer lists. Each answered "Not permitted", the answer for a wrong key. One the OS raises carries an
            # errno (a state file the process may not read): the server's fault, a 500. As the create's, and the
            # FileNotFoundError below.
            if exc.errno is not None:
                raise
            log_hidden_cause(logger, 409, exc, "cron trigger refused for job %s", job_id)
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_RUN_REFUSED_DETAIL) from None
        except AgentJobQueueFull:  # and only that: any other RuntimeError is a fault. The run's session is released.
            raise queue_full(logger, "a run of cron job", payload.job_id) from None
        except FileNotFoundError as exc:
            # A lookup's own (the job's auth profile) has no errno; one the OS raises names a server path and is not
            # a state the caller can change.
            if exc.errno is not None:
                raise
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_AUTH_PROFILE_DETAIL) from None
        except CronJobDisabledError as exc:
            raise HTTPException(status_code=409, detail=error_text(exc)) from None
        except BrowserUnreachableError:  # the run's session could not reach its browser (create_session)
            raise HTTPException(status_code=503, detail=BROWSER_UNREACHABLE_DETAIL) from None
        except BrowserActionError as exc:
            # What the run's session answers its own caller, which the app's handler passed on as it was: 403 with
            # the refused host for a start_url the pre-request gate stopped, 400 for one that did not load, 503 in
            # words that name a setting for a browser whose gate or window watcher is not attached. Here a 403 is the
            # key's and a 400 the request's, and the caller is told no host: one that may work at the next try
            # answers 503, which a webhook's sender tries again, and any other the fixed 409.
            if exc.retryable:
                log_hidden_cause(logger, 503, exc, "cron trigger failed for job %s", job_id)
                raise HTTPException(status_code=503, detail=CRON_TRIGGER_TRY_AGAIN_DETAIL) from None
            log_hidden_cause(logger, 409, exc, "cron trigger refused for job %s", job_id)
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_RUN_REFUSED_DETAIL) from None
        except SessionLimitError:  # the run's session (CB1-8), with a detail that names none (W2-14)
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_SESSION_LIMIT_DETAIL) from None
        except AuthStateDecryptError as exc:
            # A fixed text that says what to check; it names no path.
            raise HTTPException(status_code=422, detail=error_text(exc)) from None
        except (KeyError, ValueError) as exc:
            # The job as it is stored: a record with no goal (it answered 404 "Not found"), one the run's request
            # model refuses (400 "Invalid request"), one that holds a list where a run reads text (an unhandled 500:
            # the service refuses it as a ValueError). None is in the caller's hands.
            log_hidden_cause(logger, 409, exc, "cron trigger failed for job %s", job_id)
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_RUN_REFUSED_DETAIL) from None

    return router
