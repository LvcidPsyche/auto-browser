"""A NaN or an infinity in a request body is refused naming its field, where it answered 500 or reached the browser.

Python's JSON reader takes the bare words NaN, Infinity and -Infinity, and a number too large for a float (1e999),
as floats. Two things then went wrong. A body refused for any reason is answered 422 with what was sent, and FastAPI
writes that answer as strict JSON: with such a number in it the answer itself failed, a 500. And a coordinate or a
scroll distance, which is a float, took one: it reached Playwright, which refused it, and the caller was told the
action had failed and could be retried.
"""

from __future__ import annotations

import json
import math
import unittest
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
from app.config import Settings
from app.models import McpToolCallRequest, StrictInputModel
from app.tool_gateway.gateway import McpToolGateway

JSON = {"content-type": "application/json"}
NON_FINITE = (b"NaN", b"Infinity", b"-Infinity", b"1e999")
# A lone surrogate, as a JSON escape for one decodes to: UTF-8 cannot encode it (tests/test_lone_surrogate_records).
LONE_ESCAPE = b'"a\\ud800"'


def _strict_loads(text: str) -> Any:
    def refuse(constant: str) -> None:
        raise ValueError(f"invalid JSON constant {constant}")

    return json.loads(text, parse_constant=refuse)


def _request_models(model: type[StrictInputModel] = StrictInputModel) -> Iterator[type[StrictInputModel]]:
    for sub in model.__subclasses__():
        yield sub
        yield from _request_models(sub)


class RestBodyTests(unittest.TestCase):
    """Through the real app, as tests/test_non_finite_responses.py goes."""

    def setUp(self) -> None:
        self.manager = main_module.manager
        calls = {
            name: AsyncMock(return_value={"ok": True}) for name in ("click", "hover", "scroll", "execute_decision")
        }
        for patcher in (
            patch.object(self.manager, "ensure_session_accessible", new=AsyncMock()),
            patch.multiple(self.manager, **calls),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = calls
        self.client = TestClient(main_module.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    def post(self, path: str, body: bytes) -> int:
        return self.client.post(path, content=body, headers=JSON).status_code

    def assert_refused(self, path: str, body: bytes, *field: str | int, kind: str | None = None) -> dict[str, Any]:
        response = self.client.post(path, content=body, headers=JSON)
        self.assertEqual(response.status_code, 422, (body, response.text[:200]))
        answer = _strict_loads(response.text)  # the answer is JSON a strict parser reads
        errors = {tuple(error["loc"]): error for error in answer["detail"]}
        self.assertIn(("body", *field), errors, (body, answer))
        error = errors[("body", *field)]
        if kind is not None:
            self.assertEqual(error["type"], kind, (body, answer))
        return error

    def test_a_non_finite_number_in_an_integer_field_answers_422_naming_the_field(self) -> None:
        for number in NON_FINITE:
            with self.subTest(number=number):
                error = self.assert_refused(
                    "/sessions/s1/observe", b'{"limit": ' + number + b"}", "limit", kind="finite_number"
                )

                self.assertEqual(error["msg"], "Input should be a finite number")
                self.assertIsNone(error["input"])  # as any answer writes one (tests/test_non_finite_responses.py)

    def test_a_body_refused_for_another_reason_is_still_answered_when_it_holds_one(self) -> None:
        # Each 422 quotes what was sent: the field's value, the whole body, the object the unknown field sits in.
        for path, body, field in (
            ("/sessions/s1/observe", b'{"preset": NaN}', ("preset",)),
            ("/sessions/s1/observe", b"[NaN, 1]", ()),
            ("/sessions/s1/observe", b'{"nope": -Infinity}', ("nope",)),
            ("/sessions/s1/observe", b'{"limit": "x", "preset": {"deep": [1e999]}}', ("limit",)),
            ("/mcp/tools/call", b'{"name": "browser.observe", "arguments": NaN}', ("arguments",)),
        ):
            with self.subTest(body=body):
                self.assert_refused(path, body, *field)

    def test_a_refused_body_holding_one_and_a_lone_surrogate_is_answered_too(self) -> None:
        body = b'{"limit": NaN, "preset": 5, "nope": ' + LONE_ESCAPE + b"}"

        error = self.assert_refused("/sessions/s1/observe", body, "nope", kind="extra_forbidden")

        self.assertEqual(error["input"], "a�")
        self.assert_refused("/sessions/s1/observe", body, "limit", kind="finite_number")

    def test_a_non_finite_coordinate_or_distance_answers_422_and_reaches_no_action(self) -> None:
        for path, template, field in (
            ("/sessions/s1/actions/click", b'{"x": %s, "y": 1}', ("x",)),
            ("/sessions/s1/actions/click", b'{"x": 1, "y": %s}', ("y",)),
            ("/sessions/s1/actions/hover", b'{"x": %s, "y": 1}', ("x",)),
            ("/sessions/s1/actions/hover", b'{"x": 1, "y": %s}', ("y",)),
            ("/sessions/s1/actions/scroll", b'{"delta_x": %s}', ("delta_x",)),
            ("/sessions/s1/actions/scroll", b'{"delta_y": %s}', ("delta_y",)),
            (
                "/sessions/s1/actions/execute",
                b'{"action": {"action": "click", "reason": "r", "x": %s, "y": 2}}',
                ("action", "x"),
            ),
            (
                "/sessions/s1/actions/execute",
                b'{"action": {"action": "scroll", "reason": "r", "delta_y": %s}}',
                ("action", "delta_y"),
            ),
        ):
            for number in NON_FINITE:
                with self.subTest(path=path, field=field, number=number):
                    self.assert_refused(path, template % number, *field, kind="finite_number")
        for call in self.calls.values():
            call.assert_not_awaited()

    def test_a_finite_coordinate_and_distance_still_reach_their_action(self) -> None:
        # The other half of the test above: these bodies are refused for their number, and nothing else.
        self.assertEqual(self.post("/sessions/s1/actions/click", b'{"x": 1.5, "y": 2}'), 200)
        self.assertEqual(self.post("/sessions/s1/actions/hover", b'{"x": 1e3, "y": 0}'), 200)
        self.assertEqual(self.post("/sessions/s1/actions/scroll", b'{"delta_x": -0.5, "delta_y": 1e308}'), 200)
        body = b'{"action": {"action": "scroll", "reason": "r", "delta_y": -250.5}}'
        self.assertEqual(self.post("/sessions/s1/actions/execute", body), 200)

        self.assertEqual(self.calls["click"].await_args.kwargs["x"], 1.5)
        self.assertEqual(self.calls["hover"].await_args.kwargs["x"], 1000.0)
        self.assertEqual(self.calls["scroll"].await_args.args[1:], (-0.5, 1e308))
        self.assertEqual(self.calls["execute_decision"].await_count, 1)


class McpArgumentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = SimpleNamespace(
            settings=Settings(_env_file=None),
            ensure_session_accessible=AsyncMock(),
            execute_decision=AsyncMock(return_value={"ok": True}),
        )
        self.gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )
        # The drag itself (app/tool_gateway/drag.py) works a page's mouse: what is asked here is whether it is reached.
        patcher = patch("app.tool_gateway.gateway.drag_drop", new=AsyncMock(return_value={"ok": True}))
        self.drag_drop = patcher.start()
        self.addCleanup(patcher.stop)

    async def call(self, name: str, **arguments: Any):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments={"session_id": "s1", **arguments}))

    async def test_a_non_finite_coordinate_is_an_error_naming_the_field(self) -> None:
        for number in (math.nan, math.inf, -math.inf):
            for name, arguments, field in (
                (
                    "browser.execute_action",
                    {"action": {"action": "click", "reason": "r", "x": number, "y": 2}},
                    "action.x",
                ),
                (
                    "browser.execute_action",
                    {"action": {"action": "hover", "reason": "r", "x": 1, "y": number}},
                    "action.y",
                ),
                (
                    "browser.execute_action",
                    {"action": {"action": "scroll", "reason": "r", "delta_x": number}},
                    "action.delta_x",
                ),
                (
                    "browser.execute_action",
                    {"action": {"action": "scroll", "reason": "r", "delta_y": number}},
                    "action.delta_y",
                ),
                ("browser.drag_drop", {"source_x": number, "source_y": 1, "target_x": 5, "target_y": 5}, "source_x"),
                ("browser.drag_drop", {"source_x": 1, "source_y": number, "target_x": 5, "target_y": 5}, "source_y"),
                ("browser.drag_drop", {"source_x": 1, "source_y": 1, "target_x": number, "target_y": 5}, "target_x"),
                ("browser.drag_drop", {"source_x": 1, "source_y": 1, "target_x": 5, "target_y": number}, "target_y"),
            ):
                with self.subTest(tool=name, field=field, number=number):
                    response = await self.call(name, **arguments)

                    self.assertTrue(response.isError)
                    self.assertEqual(
                        response.content[0].text,
                        f"Invalid arguments for {name}: {field}: Input should be a finite number",
                    )
        self.manager.execute_decision.assert_not_awaited()
        self.drag_drop.assert_not_awaited()

    async def test_finite_coordinates_still_reach_the_action(self) -> None:
        scrolled = await self.call(
            "browser.execute_action", action={"action": "scroll", "reason": "r", "delta_y": 1e308}
        )
        dragged = await self.call("browser.drag_drop", source_x=1.5, source_y=1, target_x=5, target_y=5)

        self.assertFalse(scrolled.isError, scrolled.content[0].text)
        self.assertFalse(dragged.isError, dragged.content[0].text)
        self.manager.execute_decision.assert_awaited_once()
        self.drag_drop.assert_awaited_once()


class RequestModelTests(unittest.TestCase):
    def test_every_request_models_float_field_refuses_a_non_finite_number(self) -> None:
        # Whatever is added later: a request model is a StrictInputModel, and the refusal is its own.
        checked = []
        for model in set(_request_models()):
            for name, field in model.model_fields.items():
                if "float" not in str(field.annotation):
                    continue
                for number in (math.nan, math.inf, -math.inf):
                    with self.subTest(model=model.__name__, field=name, number=number):
                        with self.assertRaises(ValidationError) as refused:
                            model.model_validate({name: number})

                        errors = {error["loc"]: error["type"] for error in refused.exception.errors()}
                        self.assertIn((name,), errors)
                        # confidence is refused by its range (0 to 1), which a NaN is not in either.
                        self.assertIn(errors[(name,)], {"finite_number", "greater_than_equal", "less_than_equal"})
                checked.append(f"{model.__name__}.{name}")

        self.assertIn("ClickRequest.x", checked)
        self.assertIn("BrowserActionDecision.delta_y", checked)
        self.assertIn("DragDropInput.target_y", checked)


if __name__ == "__main__":
    unittest.main()
