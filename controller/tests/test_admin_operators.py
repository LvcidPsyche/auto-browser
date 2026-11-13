"""The controller-wide surfaces are the listed operators' once API_ADMIN_OPERATORS lists any (AUTH-32).

Named credentials (API_BEARER_TOKENS) keep each operator's sessions, approvals and jobs apart. Some surfaces belong
to no operator, and any credential could use them: the maintenance sweep and its status, the readiness report, the
proxy personas and the mesh peer list. API_ADMIN_OPERATORS lists the operators who may: a caller passes when a named
credential proved its id and the id is listed. Unset, every credential passes, as before. The shared
API_BEARER_TOKEN proves no operator, so it is refused there once the list is set, whatever its operator header
says. The MCP tools that reach the same operations ask the same function, so a refused call changes nothing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.admin_operators import ADMIN_OPERATOR_REQUIRED, AdminOperatorRequired, require_admin_operator
from app.app_factory import build_controller_services, create_controller_app
from app.config import Settings
from app.middleware.http import install_controller_http_middleware
from app.models import OperatorIdentity
from app.routes.mcp import create_mcp_router
from app.routes.operations import create_operations_router
from app.session_ownership import acting_as, as_system
from tests.test_browser_manager_create_session import _settings
from tests.test_session_ownership import ALICE, ALICE_BY_HEADER, BOB, _NoLifespan

ALICE_TOKEN = "a" * 40
BOB_TOKEN = "b" * 40
SHARED_TOKEN = "s" * 40


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ADMIN = _bearer(ALICE_TOKEN)
# Who is refused once alice alone is listed. A header's "alice" is a label, not a proof.
OTHERS = {
    "another operator's named token": _bearer(BOB_TOKEN),
    "the shared token": _bearer(SHARED_TOKEN),
    "the shared token, with alice in its operator header": {**_bearer(SHARED_TOKEN), "X-Operator-Id": "alice"},
}
# A persona nobody owns, which any operator could read, overwrite and delete.
OPEN_PERSONA = "open"
NEW_PERSONA = {"name": "new", "server": "http://new.test:3128"}
# Every REST route of the admin set, the reads first: the last one deletes the persona the others read.
ADMIN_ROUTES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/maintenance/status", None),
    ("POST", "/maintenance/cleanup", None),
    ("GET", "/readiness", None),
    ("GET", "/mesh/peers", None),
    ("GET", "/proxy-personas", None),
    ("GET", f"/proxy-personas/{OPEN_PERSONA}", None),
    ("POST", "/proxy-personas", NEW_PERSONA),
    ("DELETE", f"/proxy-personas/{OPEN_PERSONA}", None),
)
# Every MCP tool that reaches one of those operations.
ADMIN_TOOLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("browser.readiness_check", {}),
    ("browser.list_proxy_personas", {}),
    ("browser.create_proxy_persona", NEW_PERSONA),
    ("browser.delete_proxy_persona", {"name": OPEN_PERSONA}),
)
REFUSED_TOOL = {"error": ADMIN_OPERATOR_REQUIRED, "code": "not_permitted"}


class _Controller(unittest.TestCase):
    """The controller's own app and middleware, with two named credentials and the shared token."""

    ADMINS = "alice"

    def setUp(self) -> None:
        self.build()

    def build(self) -> None:
        """A controller and a persona file of its own: what one caller saves is not another's conflict."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.personas = root / "personas.json"
        self.personas.write_text(json.dumps({OPEN_PERSONA: {"server": "http://open.test:3128"}}), encoding="utf-8")
        settings = _settings(root).model_copy(
            update={
                "api_bearer_token": SHARED_TOKEN,
                "api_bearer_tokens": f"alice:{ALICE_TOKEN},bob:{BOB_TOKEN}",
                "api_admin_operators": self.ADMINS,
                "proxy_persona_file": str(self.personas),
                "mcp_tool_profile": "full",
            }
        )
        services = build_controller_services(settings, version="test")
        self.cleanup = services.maintenance.run_cleanup = AsyncMock(return_value={"removed": 0})
        application = create_controller_app(services=services, version="test", lifespan=_NoLifespan)
        # The two routers app.main adds that these surfaces live in, built as it builds them.
        application.include_router(
            create_mcp_router(mcp_transport=services.mcp_transport, tool_gateway=services.tool_gateway)
        )
        application.include_router(
            create_operations_router(
                manager=services.manager, proxy_store=services.proxy_store, cron_service=services.cron_service
            )
        )
        # The lifespan's, which does not run here.
        application.state.peer_registry = SimpleNamespace(all=lambda: [])
        application.state.mesh_identity = SimpleNamespace(node_id="node", pubkey_b64="key")
        install_controller_http_middleware(
            application, settings=settings, rate_limiter=None, metrics=SimpleNamespace(enabled=False)
        )
        self.client = TestClient(application)

    def route(self, caller: dict[str, str], method: str, path: str, body: dict[str, Any] | None) -> Any:
        return self.client.request(method, path, json=body, headers=caller)

    def tool(self, caller: dict[str, str], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/mcp/tools/call", json={"name": name, "arguments": arguments}, headers=caller)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def stored(self) -> bytes:
        return self.personas.read_bytes()

    def assert_every_route_answers(self, caller: dict[str, str]) -> None:
        for method, path, body in ADMIN_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                answer = self.route(caller, method, path, body)
                # /readiness answers 503 with its report when a check fails, as some do in this bare setup.
                self.assertIn(answer.status_code, (200, 503) if path == "/readiness" else (200,), answer.text)
        self.cleanup.assert_awaited_once()
        self.assertEqual(sorted(json.loads(self.stored())), [NEW_PERSONA["name"]])

    def assert_every_tool_answers(self, caller: dict[str, str]) -> None:
        for name, arguments in ADMIN_TOOLS:
            with self.subTest(tool=name):
                self.assertFalse(self.tool(caller, name, arguments)["isError"])
        self.assertEqual(sorted(json.loads(self.stored())), [NEW_PERSONA["name"]])


class ListedOperatorsTests(_Controller):
    def test_the_maintenance_sweep_is_the_listed_operators_alone(self) -> None:
        for who, caller in OTHERS.items():
            with self.subTest(who=who):
                refused = self.client.post("/maintenance/cleanup", headers=caller)
                self.assertEqual((refused.status_code, refused.json()), (403, {"detail": ADMIN_OPERATOR_REQUIRED}))
        self.cleanup.assert_not_awaited()

        swept = self.client.post("/maintenance/cleanup", headers=ADMIN)

        self.assertEqual((swept.status_code, swept.json()), (200, {"removed": 0}))
        self.cleanup.assert_awaited_once()

    def test_every_admin_route_refuses_everyone_else_and_changes_nothing(self) -> None:
        before = self.stored()
        for method, path, body in ADMIN_ROUTES:
            for who, caller in OTHERS.items():
                with self.subTest(route=f"{method} {path}", who=who):
                    refused = self.route(caller, method, path, body)
                    self.assertEqual((refused.status_code, refused.json()), (403, {"detail": ADMIN_OPERATOR_REQUIRED}))
        self.assertEqual(self.stored(), before)
        self.cleanup.assert_not_awaited()

    def test_every_admin_route_answers_a_listed_operator(self) -> None:
        self.assert_every_route_answers(ADMIN)

    def test_a_refused_tool_call_is_a_tool_error_that_leaves_the_store_as_it_was(self) -> None:
        before = self.stored()
        for name, arguments in ADMIN_TOOLS:
            for who, caller in OTHERS.items():
                with self.subTest(tool=name, who=who):
                    refused = self.tool(caller, name, arguments)
                    self.assertTrue(refused["isError"])
                    self.assertEqual(refused["structuredContent"], REFUSED_TOOL)
        self.assertEqual(self.stored(), before)

    def test_every_admin_tool_answers_a_listed_operator(self) -> None:
        self.assert_every_tool_answers(ADMIN)

    def test_what_is_no_admin_surface_stays_every_credentials(self) -> None:
        for path in ("/operator", "/mesh/identity", "/approvals", "/pii-scrubber", "/mcp/tools"):
            for who, caller in OTHERS.items():
                with self.subTest(path=path, who=who):
                    self.assertEqual(self.client.get(path, headers=caller).status_code, 200)


class NobodyListedTests(_Controller):
    """Unset, the surfaces are every credential's, as they were."""

    ADMINS = ""

    def test_every_admin_route_answers_every_credential(self) -> None:
        for who, caller in {"alice": ADMIN, **OTHERS}.items():
            with self.subTest(who=who):
                self.build()
                self.assert_every_route_answers(caller)

    def test_every_admin_tool_answers_every_credential(self) -> None:
        for who, caller in {"alice": ADMIN, **OTHERS}.items():
            with self.subTest(who=who):
                self.build()
                self.assert_every_tool_answers(caller)


class RequireAdminOperatorTests(unittest.TestCase):
    """The one check the routes and the tools share."""

    def passes(self, listed: str, operator: OperatorIdentity | None) -> bool:
        settings = Settings(_env_file=None, API_ADMIN_OPERATORS=listed)
        try:
            with acting_as(operator):
                require_admin_operator(settings)
        except AdminOperatorRequired as refused:
            self.assertEqual(str(refused), ADMIN_OPERATOR_REQUIRED)
            self.assertIsInstance(refused, PermissionError)
            return False
        return True

    def test_a_listed_id_passes_only_when_a_named_credential_proved_it(self) -> None:
        self.assertTrue(self.passes("alice", ALICE))
        for who, operator in (("another operator", BOB), ("a header", ALICE_BY_HEADER), ("no operator", None)):
            with self.subTest(who=who):
                self.assertFalse(self.passes("alice", operator))

    def test_the_system_is_no_admin(self) -> None:
        # Housekeeping reaches none of these surfaces through a route or a tool: it calls the service.
        with as_system():
            self.assertFalse(self.passes("alice", None))

    def test_ids_are_split_at_commas_trimmed_and_compared_exactly(self) -> None:
        self.assertTrue(self.passes(" alice , ,bob ", BOB))
        self.assertFalse(self.passes("Alice", ALICE))
        self.assertEqual(
            Settings(_env_file=None, API_ADMIN_OPERATORS=" alice , ,bob ").api_admin_operator_ids, {"alice", "bob"}
        )

    def test_a_list_that_names_nobody_refuses_nobody(self) -> None:
        for listed in ("", " ", " , "):
            with self.subTest(listed=listed):
                self.assertTrue(self.passes(listed, None))
                self.assertTrue(self.passes(listed, BOB))

    def test_the_refusal_names_the_setting_and_no_operator(self) -> None:
        self.assertIn("API_ADMIN_OPERATORS", ADMIN_OPERATOR_REQUIRED)
        self.assertNotIn("alice", ADMIN_OPERATOR_REQUIRED)


if __name__ == "__main__":
    unittest.main()
