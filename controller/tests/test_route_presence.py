"""Route-presence smoke test — the machine-enforced guard for router mounting.

FastAPI has twice been suspected of changing ``include_router`` internals in a
way that drops mounted routes *silently*: the app still boots, ``/docs`` still
renders, and every endpoint 404s. Nothing in a normal unit test suite fails that
way by accident, which is why ``controller/requirements.txt`` carried a blind
version cap instead of a test.

This is that test. It asserts three independent layers so a regression cannot
slip through whichever one a future upstream change happens to spare:

1. every ``include_router`` call site is represented in the OpenAPI surface,
2. the total surface has not collapsed,
3. the canaries actually dispatch through the router at runtime.

Layer 3 matters on its own: since FastAPI 0.137 ``app.routes`` holds
``_IncludedRouter`` wrapper objects rather than flattened ``Route`` objects, so
introspection and dispatch can diverge. Never enumerate ``app.routes`` expecting
a ``.path`` attribute — use ``app.openapi()`` (layer 1) or a request (layer 3).
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="auto-browser-route-presence-"))
atexit.register(lambda: shutil.rmtree(_TEST_ROOT, ignore_errors=True))
for env_name, relative_path in {
    "ARTIFACT_ROOT": "artifacts",
    "UPLOAD_ROOT": "uploads",
    "AUTH_ROOT": "auth",
    "APPROVAL_ROOT": "approvals",
    "AUDIT_ROOT": "audit",
    "SESSION_STORE_ROOT": "sessions",
    "JOB_STORE_ROOT": "jobs",
    "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
    "CRON_STORE_PATH": "crons/crons.json",
    "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
}.items():
    os.environ.setdefault(env_name, str(_TEST_ROOT / relative_path))

import app.main as main_module  # noqa: E402

# One canary per include_router call site, mapped to the module that registers
# it. If a router stops mounting, its canary is the failure message.
ROUTER_CANARIES = {
    "/healthz": "routes/system.py (via create_controller_app)",
    "/mesh/identity": "routes/extensions/mesh.py (via register_all_routers)",
    "/sessions/{session_id}/network/requests": "routes/extensions/network.py (via register_all_routers)",
    "/sessions/{session_id}/cdp/raw": "routes/extensions/cdp.py (via register_all_routers)",
    "/workflows/runs": "routes/extensions/workflow.py (via register_all_routers)",
    "/dashboard": "routes/extensions/dashboard.py (via register_all_routers)",
    "/agent/jobs": "routes/agent.py (via main.py)",
    "/auth-profiles": "routes/auth_profiles.py (via main.py)",
    "/mcp/tools": "routes/mcp.py (via main.py)",
    "/approvals": "routes/operations.py (via main.py)",
    "/sessions/{session_id}/audit": "routes/session_diagnostics.py (via main.py)",
    "/sessions": "routes/sessions.py (via main.py)",
    "/share/{token}": "routes/share.py (via main.py)",
}

# The surface was 91 paths at v1.4.0. The floor catches a mass drop while
# tolerating ordinary endpoint churn; raise it if the surface grows a lot.
MINIMUM_ROUTE_COUNT = 80

# Concrete GET URLs that must reach a handler. Any status other than 404 proves
# the route dispatched — 401/405/500 all mean the router is mounted.
DISPATCH_PROBES = (
    "/healthz",
    "/sessions",
    "/agent/jobs",
    "/auth-profiles",
    "/approvals",
    "/mesh/identity",
    "/workflows/runs",
)


class RoutePresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = set(main_module.app.openapi().get("paths", {}))

    def test_every_router_is_mounted(self) -> None:
        for path, origin in ROUTER_CANARIES.items():
            with self.subTest(path=path):
                self.assertIn(
                    path,
                    self.paths,
                    f"{path} is missing from the OpenAPI surface — {origin} did not mount. "
                    "include_router may be silently dropping routes on this FastAPI version.",
                )

    def test_route_surface_has_not_collapsed(self) -> None:
        self.assertGreaterEqual(
            len(self.paths),
            MINIMUM_ROUTE_COUNT,
            f"OpenAPI surface collapsed to {len(self.paths)} paths (floor {MINIMUM_ROUTE_COUNT}). "
            "A silent include_router regression leaves only FastAPI's default doc routes.",
        )


class RouteDispatchTests(unittest.TestCase):
    """Layer 3: the routes must dispatch, not merely appear in the schema."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service, method_name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.client = self.stack.enter_context(TestClient(main_module.app, raise_server_exceptions=False))

    def tearDown(self) -> None:
        self.stack.close()

    def test_canary_routes_dispatch(self) -> None:
        for path in DISPATCH_PROBES:
            with self.subTest(path=path):
                self.assertNotEqual(
                    self.client.get(path).status_code,
                    404,
                    f"GET {path} returned 404 — the route is in the schema but does not dispatch.",
                )


if __name__ == "__main__":
    unittest.main()
