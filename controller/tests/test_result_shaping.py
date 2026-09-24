"""MCP results carry what a model needs to act, not the operator's full records.

Every session reference in a tool result was the whole session record (~1.9k
characters of isolation roots, auth-state metadata and witness status), and an
action result carried the page before and after the action, each with its own
copy. One typed field cost ~10k characters of model context. These tests pin
the compact shape, the ``detail="full"`` escape hatch, and the rule that a
compact result is a subset of the full one.
"""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from app.models import McpToolCallRequest, SessionRecord
from app.providers.base import BaseProviderAdapter
from app.result_shaping import (
    SESSION_REFERENCE_KEYS,
    compact_action_result,
    compact_observation,
    compact_session,
    shape_mcp_result,
)
from app.tool_gateway import McpToolGateway


def _session_summary(session_id: str = "sess-1", *, status: str = "active", live: bool = True) -> dict[str, Any]:
    """A summary with every key BrowserSessionService.summary produces."""
    return {
        "id": session_id,
        "name": "demo",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01Z",
        "status": status,
        "live": live,
        "current_url": "https://example.com/login",
        "title": "Sign in",
        "artifact_dir": f"/data/artifacts/{session_id}",
        "takeover_url": "http://127.0.0.1:6080/vnc.html",
        "remote_access": {"active": False, "status": "inactive", "info_path": "/data/tunnels/reverse-ssh.json"},
        "isolation": {"mode": "shared_browser_node", "state_roots": {"auth_dir": f"/data/auth/{session_id}"}},
        "auth_state": {"path": None, "encryption_enabled": True, "session_auth_root": f"/data/auth/{session_id}"},
        "downloads": [],
        "last_action": "type",
        "trace_path": f"/data/artifacts/{session_id}/trace.zip",
        "proxy_persona": None,
        "protection_mode": "normal",
        "witness_remote": {"configured": False, "status": "disabled"},
    }


def _observation() -> dict[str, Any]:
    return {
        "session": _session_summary(),
        "url": "https://example.com/login",
        "title": "Sign in",
        "active_element": {"tag": "input", "element_id": "op-1", "label": "Email"},
        "text_excerpt": "Sign in to Acme",
        "dom_outline": {"counts": {"inputs": 2, "buttons": 1}},
        "accessibility_outline": {"available": False, "nodes": []},
        "ocr": None,
        "interactables": [
            {
                "element_id": "op-1",
                "selector_hint": '[data-operator-id="op-1"]',
                "tag": "input",
                "type": "email",
                "role": "textbox",
                "label": "Email",
                "checked": None,
                "disabled": False,
                "href": None,
                "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
            },
            {
                "element_id": "op-2",
                "selector_hint": '[data-operator-id="op-2"]',
                "tag": "input",
                "type": "checkbox",
                "role": "checkbox",
                "label": "Remember me",
                "checked": False,
                "disabled": True,
                "href": None,
                "bbox": {"x": 5, "y": 6, "width": 7, "height": 8},
            },
        ],
        "screenshot_path": "/data/artifacts/sess-1/observe.png",
        "screenshot_url": "/artifacts/sess-1/observe.png",
        "console_messages": [],
        "page_errors": [],
        "request_failures": [],
        "tabs": [{"index": 0, "active": True, "url": "https://example.com/login", "title": "Sign in"}],
        "recent_downloads": [],
        "takeover_url": "http://127.0.0.1:6080/vnc.html",
        "remote_access": {"active": False, "status": "inactive", "info_path": "/data/tunnels/reverse-ssh.json"},
        "preset": "normal",
    }


def _action_result() -> dict[str, Any]:
    return {
        "action": "type",
        "action_class": "write",
        "session": _session_summary(),
        "before": {"url": "https://example.com/login", "text_excerpt": "Sign in to Acme", "dom_outline": {}},
        "after": _observation(),
        "target": {"element_id": "op-1", "text_length": 17},
        "verification": {"verified": True, "signals": ["active_element_changed"]},
    }


def _assert_subset(test: unittest.TestCase, compact: Any, full: Any, path: str = "$") -> None:
    """Every key the compact result keeps has the same value (or a subset of it) in the full one."""
    if isinstance(compact, dict):
        test.assertIsInstance(full, dict, path)
        for key, value in compact.items():
            test.assertIn(key, full, f"{path}.{key} is not in the full result")
            _assert_subset(test, value, full[key], f"{path}.{key}")
    elif isinstance(compact, list):
        test.assertIsInstance(full, list, path)
        test.assertEqual(len(compact), len(full), path)
        for index, (item, full_item) in enumerate(zip(compact, full, strict=True)):
            _assert_subset(test, item, full_item, f"{path}[{index}]")
    else:
        test.assertEqual(compact, full, path)


class ProjectionTests(unittest.TestCase):
    def test_reference_keys_exist_on_the_session_record(self) -> None:
        # The persisted record is the summary validated; a renamed field would
        # otherwise silently vanish from every compact reference.
        self.assertLessEqual(set(SESSION_REFERENCE_KEYS), set(SessionRecord.model_fields))

    def test_session_summary_becomes_a_reference(self) -> None:
        compact = compact_session(_session_summary())

        self.assertEqual(tuple(compact), SESSION_REFERENCE_KEYS)
        self.assertEqual(compact["id"], "sess-1")
        self.assertTrue(compact["live"])

    def test_values_that_are_not_session_summaries_pass_through(self) -> None:
        for value in ("sess-1", None, {"id": "sess-1"}, ["sess-1"]):
            with self.subTest(value=value):
                self.assertEqual(compact_session(value), value)

    def test_observation_drops_operator_metadata_only(self) -> None:
        full = _observation()
        compact = compact_observation(copy.deepcopy(full))

        self.assertNotIn("remote_access", compact)
        self.assertEqual(set(compact["session"]), set(SESSION_REFERENCE_KEYS))
        self.assertEqual(set(full) - set(compact), {"remote_access"})
        for key in ("url", "title", "screenshot_url", "screenshot_path", "takeover_url", "text_excerpt", "tabs"):
            self.assertEqual(compact[key], full[key], key)

    def test_interactables_lose_inapplicable_keys_but_keep_state(self) -> None:
        email, checkbox = compact_observation(_observation())["interactables"]

        self.assertNotIn("checked", email)
        self.assertNotIn("href", email)
        self.assertIs(email["disabled"], False)
        self.assertIs(checkbox["checked"], False)
        self.assertIs(checkbox["disabled"], True)
        # wait_for_selector and drag_drop take selectors only, so the hint stays.
        self.assertEqual(email["selector_hint"], '[data-operator-id="op-1"]')

    def test_action_result_keeps_outcome_and_page_after(self) -> None:
        full = _action_result()
        compact = compact_action_result(copy.deepcopy(full))

        self.assertNotIn("before", compact)
        self.assertNotIn("session", compact["after"])
        self.assertNotIn("remote_access", compact["after"])
        for key in ("action", "action_class", "target", "verification"):
            self.assertEqual(compact[key], full[key], key)
        self.assertEqual(compact["after"]["url"], "https://example.com/login")
        self.assertEqual(compact["session"]["id"], "sess-1")

    def test_action_results_without_a_page_after_are_left_alone(self) -> None:
        # e.g. an upload that returned early; only a nested summary is reduced.
        result = {"action": "upload", "status": "queued", "session": _session_summary()}

        compact = compact_action_result(copy.deepcopy(result))

        self.assertEqual(compact["status"], "queued")
        self.assertEqual(set(compact["session"]), set(SESSION_REFERENCE_KEYS))

    def test_compact_results_are_subsets_of_full_results(self) -> None:
        cases = {
            "browser.observe": _observation(),
            "browser.execute_action": _action_result(),
            "browser.execute_approval": {"approval": {"id": "a-1"}, "execution": _action_result()},
            "browser.list_sessions": [_session_summary("a"), _session_summary("b", status="closed", live=False)],
            "browser.get_console": {"session": _session_summary(), "items": [{"type": "error", "text": "boom"}]},
        }
        for tool, full in cases.items():
            with self.subTest(tool=tool):
                compact = shape_mcp_result(tool, copy.deepcopy(full))
                self.assertNotEqual(compact, full)
                _assert_subset(self, compact, full)

    def test_full_detail_and_top_level_records_are_untouched(self) -> None:
        for tool, result, detail in (
            ("browser.observe", _observation(), "full"),
            ("browser.execute_action", _action_result(), "full"),
            ("browser.get_session", _session_summary(), "compact"),
            ("browser.create_session", _session_summary(), "compact"),
            ("browser.execute_approval", {"approval": {"id": "a-1", "status": "executed"}}, "compact"),
        ):
            with self.subTest(tool=tool, detail=detail):
                self.assertEqual(shape_mcp_result(tool, copy.deepcopy(result), detail=detail), result)

    def test_provider_prompt_uses_the_session_reference(self) -> None:
        compact = BaseProviderAdapter.compact_observation(_observation())

        self.assertEqual(set(compact["session"]), set(SESSION_REFERENCE_KEYS))


class GatewayShapingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = SimpleNamespace(
            list_sessions=AsyncMock(return_value=[_session_summary()]),
            get_session_record=AsyncMock(return_value=_session_summary()),
            observe=AsyncMock(side_effect=lambda *args, **kwargs: _observation()),
            execute_decision=AsyncMock(side_effect=lambda *args, **kwargs: _action_result()),
            execute_approval=AsyncMock(
                side_effect=lambda *args, **kwargs: {"approval": {"id": "a-1"}, "execution": _action_result()}
            ),
            get_console_messages=AsyncMock(
                return_value={"session": _session_summary(), "items": [{"type": "log", "text": "hi"}]}
            ),
            require_governed_approval=AsyncMock(return_value=None),
        )
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",  # browser.execute_approval is not in the curated set
        )

    async def _call(self, name: str, **arguments: Any):
        response = await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))
        self.assertFalse(response.isError, response.content[0].text)
        # The text block is what most clients show the model, and what the
        # LangChain adapter parses; it must match the structured result.
        self.assertEqual(json.loads(response.content[0].text), response.structuredContent)
        return response.structuredContent

    async def test_observe_is_compact_by_default(self) -> None:
        result = await self._call("browser.observe", session_id="sess-1")

        self.assertEqual(set(result["session"]), set(SESSION_REFERENCE_KEYS))
        self.assertNotIn("remote_access", result)
        self.assertEqual(result["url"], "https://example.com/login")
        self.assertEqual(result["screenshot_url"], "/artifacts/sess-1/observe.png")
        # detail shapes the response only; it is not an observe option.
        self.manager.observe.assert_awaited_once_with("sess-1", limit=40, preset=None)

    async def test_observe_full_detail_returns_the_manager_payload(self) -> None:
        result = await self._call("browser.observe", session_id="sess-1", detail="full")

        self.assertEqual(result, _observation())

    async def test_execute_action_is_compact_by_default(self) -> None:
        action = {"action": "type", "element_id": "op-1", "text": "a@b.co", "reason": "fill", "risk_category": "write"}

        compact = await self._call("browser.execute_action", session_id="sess-1", action=action)
        full = await self._call("browser.execute_action", session_id="sess-1", action=action, detail="full")

        self.assertNotIn("before", compact)
        self.assertNotIn("session", compact["after"])
        self.assertEqual(full, _action_result())

    async def test_execute_approval_compacts_the_execution(self) -> None:
        result = await self._call("browser.execute_approval", approval_id="a-1")

        self.assertEqual(result["approval"], {"id": "a-1"})
        self.assertNotIn("before", result["execution"])

    async def test_session_references_elsewhere_are_compact(self) -> None:
        listed = await self._call("browser.list_sessions")
        console = await self._call("browser.get_console", session_id="sess-1")
        record = await self._call("browser.get_session", session_id="sess-1")

        self.assertEqual([set(item) for item in listed], [set(SESSION_REFERENCE_KEYS)])
        self.assertEqual(set(console["session"]), set(SESSION_REFERENCE_KEYS))
        self.assertEqual(console["items"], [{"type": "log", "text": "hi"}])
        # browser.get_session is where the full record lives.
        self.assertEqual(record, _session_summary())

    async def test_detail_is_validated(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.observe", arguments={"session_id": "sess-1", "detail": "verbose"})
        )

        self.assertTrue(response.isError)
        self.assertIn("detail", response.content[0].text)


if __name__ == "__main__":
    unittest.main()
