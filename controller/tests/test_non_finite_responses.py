"""A NaN or an infinity in a route's answer goes out as null, where it failed the response.

The app's default response class is SafeJSONResponse, which takes a typed route off FastAPI's own JSON path: that path
wrote a non-finite float as null (1.17.0), and JSONResponse's json.dumps(allow_nan=False) raises ValueError on one
after the handler has run. A page hands such numbers over (a rect of a zero-sized element, a measured ratio).
"""

from __future__ import annotations

import contextlib
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

import app.main as main_module
from app.action_errors import BrowserActionError
from app.safe_json_response import SafeJSONResponse

NAN = float("nan")
INFINITY = float("inf")
LONE = "\ud83d"
REPLACEMENT = chr(0xFFFD)


class Measured(BaseModel):
    ratio: float
    name: str = "m"


async def a_model() -> Measured:
    return Measured(ratio=NAN)


async def a_dict_holding_a_model() -> dict[str, Any]:
    return {"measured": Measured(ratio=-INFINITY), "others": [Measured(ratio=0.5)]}


class SafeJSONResponseNonFiniteTests(unittest.TestCase):
    def test_nan_and_the_infinities_are_null_at_any_depth(self) -> None:
        body = SafeJSONResponse({"a": NAN, "b": [INFINITY, -INFINITY, 1.5], "c": {"d": {"e": NAN}}, "n": 2}).body

        self.assertEqual(json.loads(body), {"a": None, "b": [None, None, 1.5], "c": {"d": {"e": None}}, "n": 2})
        self.assertNotIn(b"NaN", body)
        self.assertNotIn(b"Infinity", body)

    def test_a_body_holding_one_and_a_lone_surrogate_loses_neither_answer(self) -> None:
        body = SafeJSONResponse({"ratio": NAN, "title": f"a{LONE}b"}).body

        self.assertEqual(json.loads(body), {"ratio": None, "title": f"a{REPLACEMENT}b"})

    def test_a_finite_body_is_still_exactly_what_json_response_writes(self) -> None:
        content = {"ratio": 0.25, "big": 1e308, "small": -0.0, "nested": [1.0, {"x": 2.5}]}

        self.assertEqual(SafeJSONResponse(content).body, JSONResponse(content).body)

    def test_a_value_json_cannot_hold_still_fails_as_it_did(self) -> None:
        # Only the non-finite float is answered: an object nobody shaped is not sent as its text.
        with self.assertRaises(TypeError):
            SafeJSONResponse({"session": object()})
        with self.assertRaises(TypeError):
            SafeJSONResponse({"ratio": NAN, "session": object()})

    def test_json_dumps_other_value_error_is_raised_as_it_was(self) -> None:
        # An int too long to print (a page's BigInt can be one) is a ValueError of json.dumps too, and no null.
        with self.assertRaisesRegex(ValueError, "integer string conversion"):
            SafeJSONResponse({"n": 10**5000})


class RestRouteNonFiniteTests(unittest.TestCase):
    """Through the real app: a route goes out by the app's default class."""

    def setUp(self) -> None:
        patcher = patch.object(main_module.manager, "ensure_session_accessible", new=AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main_module.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    def mount(self, path: str, endpoint: Any) -> None:
        """A route of this test's own on the real app, so it is answered as the app answers its routes."""
        routes = main_module.app.router.routes
        mounted = len(routes)
        main_module.app.add_api_route(path, endpoint, methods=["GET"], include_in_schema=False)
        self.addCleanup(lambda: routes.__delitem__(slice(mounted, None)))

    def test_a_route_that_returns_a_model_answers_its_non_finite_float_field_as_null(self) -> None:
        # The case the default response class changed: FastAPI's own JSON path wrote null for it at 1.17.0.
        self.mount("/test-only/a-model", a_model)

        response = self.client.get("/test-only/a-model")

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.json(), {"ratio": None, "name": "m"})

    def test_a_route_that_returns_a_dict_holding_a_model_does_too(self) -> None:
        self.mount("/test-only/a-dict", a_dict_holding_a_model)

        response = self.client.get("/test-only/a-dict")

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(
            response.json(), {"measured": {"ratio": None, "name": "m"}, "others": [{"ratio": 0.5, "name": "m"}]}
        )

    def test_the_test_routes_go_out_by_the_apps_default_class(self) -> None:
        # What makes the two tests above tests of the app: a route added to it takes its default response class.
        self.mount("/test-only/a-model", a_model)

        route = main_module.app.router.routes[-1]

        self.assertEqual(route.path, "/test-only/a-model")
        self.assertIs(route.response_class, SafeJSONResponse)

    def test_a_typed_route_answers_a_non_finite_float_as_null(self) -> None:
        page = {"url": "https://x.test", "title": "t", "zoom": NAN, "rects": [{"x": INFINITY, "y": 1.5}]}

        with patch.object(main_module.manager, "observe", new=AsyncMock(return_value=page)):
            response = self.client.get("/sessions/s1/observe")

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(
            response.json(), {"url": "https://x.test", "title": "t", "zoom": None, "rects": [{"x": None, "y": 1.5}]}
        )

    def test_a_route_that_builds_its_own_answer_does_too(self) -> None:
        inspector = SimpleNamespace(entries=lambda **_: [{"url": "https://x.test/", "duration_ms": NAN}], summary=dict)

        @contextlib.asynccontextmanager
        async def no_page_to_judge(_app, _session_id):
            yield

        with (
            patch("app.routes.extensions.network._get_inspector", return_value=inspector),
            patch("app.routes.extensions.network._reading_an_allowed_page", no_page_to_judge),
        ):
            response = self.client.get("/sessions/s1/network/requests")

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertIsNone(response.json()["requests"][0]["duration_ms"])

    def test_an_action_errors_details_holding_one_keep_the_errors_status_and_code(self) -> None:
        # A BrowserActionError's details carry what was read from the page.
        error = BrowserActionError("the element moved", code="element_moved", details={"x": NAN, "y": 2.0})

        with patch.object(main_module.manager, "observe", new=AsyncMock(side_effect=error)):
            response = self.client.get("/sessions/s1/observe")

        self.assertEqual(response.status_code, error.status_code, response.text[:200])
        self.assertEqual(
            (response.json()["code"], response.json()["x"], response.json()["y"]), ("element_moved", None, 2.0)
        )


if __name__ == "__main__":
    unittest.main()
