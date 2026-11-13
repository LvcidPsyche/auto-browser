"""Who may use the surfaces no operator owns, once API_ADMIN_OPERATORS lists anyone.

Named credentials (API_BEARER_TOKENS) keep each operator's sessions, approvals, jobs and files apart
(session_ownership). Some surfaces are the whole controller's, and any credential could use them: the maintenance
sweep and its status, the readiness report, the proxy personas, and the mesh peer list. API_ADMIN_OPERATORS lists
the operators who may. A caller passes when a named credential proved its id and that id is listed, compared
exactly. Nothing else proves an operator: not the shared API_BEARER_TOKEN, whatever its operator header says, and
not the system, whose housekeeping calls the services themselves. Unset, every credential passes, as before.

One check for REST and MCP: the routes' dependency (routes/_utils.require_admin) and the tool handlers that reach
the same operations (tool_gateway) both call require_admin_operator.
"""

from __future__ import annotations

from typing import Any

from .session_ownership import verified_operator

# Fixed, and names no operator: who is listed is not for a caller who is not.
ADMIN_OPERATOR_REQUIRED = (
    "Only an operator listed in API_ADMIN_OPERATORS may do this, with a named credential of API_BEARER_TOKENS: the "
    "shared API_BEARER_TOKEN and an operator header prove no operator."
)


class AdminOperatorRequired(PermissionError):
    """The current operator is not one API_ADMIN_OPERATORS lists. A PermissionError with no errno: a route answers
    it 403 and the tool gateway as a refusal, each with its text."""


def require_admin_operator(settings: Any) -> None:
    """Raise AdminOperatorRequired unless nobody is listed or a named credential proved a listed operator."""
    listed = settings.api_admin_operator_ids
    if listed and verified_operator() not in listed:
        raise AdminOperatorRequired(ADMIN_OPERATOR_REQUIRED)
