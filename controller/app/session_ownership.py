"""Who may use a session: the operator who created it, when that operator was proven.

The rule auth profiles follow since 1.8.1, applied to sessions. A session is
owned only if a token-verified operator (a named credential from
API_BEARER_TOKENS) created it. Deployments on the shared API_BEARER_TOKEN, or
none, therefore create unowned sessions and see no change. An owned session is
invisible to every other operator: it is left out of listings and answers like
a session that does not exist, so ownership leaks nothing about who has what.

Background work that is not a request has no operator of its own. The agent job
worker and the cron service act as the operator they work for (acting_as);
controller housekeeping such as shutdown acts as the system (as_system), which
may use every session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .audit import get_current_operator, reset_current_operator, set_current_operator
from .models import OperatorIdentity

# Never produced by the HTTP middleware, so no request can claim it.
SYSTEM_SOURCE = "system"
# Whom unowned work runs as: it may use only what is unowned.
ANONYMOUS = OperatorIdentity(id="anonymous", source="anonymous")


def verified_operator() -> str | None:
    """The current operator's id, but only when it was proven by a named credential.

    `source: "header"` is a self-asserted label (see app/auth_policy.py): anyone
    able to reach the API could set it to any value, so it can never own anything.
    """
    operator = get_current_operator()
    return operator.id if operator.source == "token" else None


def may_use(owner: str | None) -> bool:
    if owner is None:
        return True
    operator = get_current_operator()
    if operator.source == SYSTEM_SOURCE:
        return True
    return operator.source == "token" and operator.id == owner


@contextmanager
def acting_as(identity: OperatorIdentity | None) -> Iterator[None]:
    """Run as a previously proven operator, such as the one who queued a job."""
    if identity is None:
        yield
        return
    token = set_current_operator(
        identity.id, name=identity.name, source=identity.source, asserted_id=identity.asserted_id
    )
    try:
        yield
    finally:
        reset_current_operator(token)


def owner_identity(owner: str | None) -> OperatorIdentity | None:
    """The identity to act as for something an operator owns, None when unowned."""
    return OperatorIdentity(id=owner, source="token") if owner else None


@contextmanager
def as_system() -> Iterator[None]:
    token = set_current_operator("system", source=SYSTEM_SOURCE)
    try:
        yield
    finally:
        reset_current_operator(token)
