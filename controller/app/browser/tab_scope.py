"""Which tab (if any) the current request is scoped to.

Set per request by ``app/middleware/tab_scope.py`` from the ``X-Tab-Id``
header, read by ``BrowserSessionService.get`` to hand back a ``TabView``.
Background work spawned while a tab-scoped request runs must not inherit the
scope -- use ``detached_context()`` when creating such tasks.
"""

from __future__ import annotations

import contextvars
import re
import secrets

TAB_ID_PATTERN = re.compile(r"^t-[0-9a-f]{12}$")
OWNER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

current_tab_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_tab_id", default=None)


def new_tab_id() -> str:
    return "t-" + secrets.token_hex(6)


def is_valid_tab_id(value: object) -> bool:
    return isinstance(value, str) and TAB_ID_PATTERN.fullmatch(value) is not None


def detached_context() -> contextvars.Context:
    """A copy of the current context with no tab scope, for background tasks."""
    ctx = contextvars.copy_context()
    ctx.run(current_tab_id.set, None)
    return ctx
