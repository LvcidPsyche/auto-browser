"""A body field FastAPI refuses is answered 422 however deep the value it refused is nested.

The 422 repeats each refused value as its error's ``input``, and FastAPI's encoder goes down a value one call a
level: a list nested 1,500 deep, which the JSON reader of Python 3.12 or 3.14 takes, made the encoder raise
RecursionError in the app's 422 handler, an unhandled 500. Where the detail cannot be encoded, the 422 now holds each
error's type, place and message, without its input. Where the JSON reader stops first (on 3.11, about 1,000 levels)
the body is the reader's 400, as before: the tests do not rest on where either edge is.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.main as main_module

JSON = {"content-type": "application/json"}
NAME_REFUSED = {"type": "string_type", "loc": ["body", "name"], "msg": "Input should be a valid string"}


def _nested(depth: int) -> bytes:
    return b'{"name": ' + b"[" * depth + b'"x"' + b"]" * depth + b"}"


class DeepRefusedFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main_module.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    def test_a_field_nested_1500_deep_is_answered_422_without_its_input(self) -> None:
        response = self.client.post("/sessions", content=_nested(1500), headers=JSON)

        if response.status_code == 400:  # the JSON reader went no deeper (3.11): its own answer
            self.assertEqual(response.json(), {"detail": "There was an error parsing the body"})
            return
        self.assertEqual(response.status_code, 422, response.text[:200])
        self.assertEqual(response.json(), {"detail": [NAME_REFUSED]})

    def test_a_detail_the_encoder_cannot_go_down_keeps_each_errors_type_place_and_message(self) -> None:
        # The encoder's RecursionError stood in for, as where it comes is the interpreter's: every error is kept, and
        # none of their inputs.
        failing = AsyncMock(side_effect=RecursionError("maximum recursion depth exceeded"))
        body = b'{"name": [1], "start_url": 5, "nope": {"deep": [1]}}'
        with patch("app.safe_json_response.request_validation_exception_handler", failing):
            response = self.client.post("/sessions", content=body, headers=JSON)

        self.assertEqual(response.status_code, 422, response.text[:200])
        detail = response.json()["detail"]
        self.assertEqual([sorted(error) for error in detail], [["loc", "msg", "type"]] * len(detail))
        self.assertIn(NAME_REFUSED, detail)
        self.assertEqual(
            sorted(tuple(error["loc"]) for error in detail),
            [("body", "name"), ("body", "nope"), ("body", "start_url")],
        )
        failing.assert_awaited_once()

    def test_a_field_nested_800_deep_is_answered_as_before_with_its_input(self) -> None:
        response = self.client.post("/sessions", content=_nested(800), headers=JSON)

        self.assertEqual(response.status_code, 422, response.text[:200])
        (error,) = response.json()["detail"]
        self.assertEqual({key: error[key] for key in NAME_REFUSED}, NAME_REFUSED)
        value, depth = error["input"], 0
        while isinstance(value, list):
            (value,), depth = value, depth + 1
        self.assertEqual((value, depth), ("x", 800))
