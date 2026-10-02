from __future__ import annotations

import re
from logging import Logger

from fastapi import HTTPException, Request

_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_safe_segment(value: str, *, field: str) -> str:
    """Validate that *value* is a single safe path segment."""
    if not isinstance(value, str) or not _SAFE_PATH_SEGMENT.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return value


async def require_session_access(request: Request) -> None:
    """Refuse another operator's session on every route that names one.

    Installed app-wide (app_factory), so routes that read a session's files
    directly (replay, witness, trace, network log) are covered as well as those
    that resolve the session through the manager. A query-string session_id
    counts too: GET /approvals?session_id=... must not list another operator's.
    """
    session_id = request.path_params.get("session_id") or request.query_params.get("session_id")
    manager = getattr(request.app.state, "browser_manager", None)
    if session_id and manager is not None:
        await manager.ensure_session_accessible(session_id)


def internal_error(logger: Logger, message: str, *args: object) -> HTTPException:
    logger.exception(message, *args)
    return HTTPException(status_code=500, detail="Internal error")
