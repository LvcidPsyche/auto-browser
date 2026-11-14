"""An answer that repeats a refused input leaves out one nested past a bound, so no walk of the answer runs out.

The REST 422 repeats each refused value as its error's ``input``, and so does MCP ``tools/call``'s -32602 ``Invalid
tools/call params``. Each walk of such an answer goes down the value by recursion: FastAPI's encoder a call a level,
and the walks that clean a lone surrogate out of it and write a NaN or an infinity as null two frames a level on
Python 3.11. A value some hundreds deep, which every Python's JSON reader takes, made one of them raise
RecursionError: an unhandled 500 (on 3.11 from about 490 levels around a lone surrogate, NaN or 1e999; on 3.12 at
980). An input nested deeper than 100 levels is now left out of its error, the depth measured by a loop; a shallower
one is repeated as it was. 600 levels is inside every Python's JSON reader, so these tests answer alike on each.
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

import app.main as main_module

JSON = {"content-type": "application/json"}
NAME_REFUSED = {"type": "string_type", "loc": ["body", "name"], "msg": "Input should be a valid string"}
# The bound, written out: the module's constant would hold the test to whatever it is.
DEPTH_BOUND = 100
# A lone surrogate (JSON's escape for one), a NaN and a number too large for a float: each sent the answer down a walk
# of its own, and "x" down only the encoder.
LEAVES = {"x": '"x"', "a lone surrogate": '"\\ud800"', "NaN": "NaN", "1e999": "1e999"}


def _nested(depth: int, leaf: str = '"x"') -> str:
    return "[" * depth + leaf + "]" * depth


def _depth(value: object) -> int:
    depth = 0
    while isinstance(value, list):
        (value,), depth = value, depth + 1
    return depth


def _client() -> TestClient:
    if main_module.rate_limiter is not None:
        main_module.rate_limiter._events.clear()
    return TestClient(main_module.app, base_url="http://127.0.0.1", raise_server_exceptions=False)


class DeepRefusedFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def _refused(self, value: str) -> dict:
        response = self.client.post("/sessions", content=('{"name": ' + value + "}").encode(), headers=JSON)
        self.assertEqual(response.status_code, 422, response.text[:200])
        (error,) = response.json()["detail"]
        self.assertEqual({key: error[key] for key in NAME_REFUSED}, NAME_REFUSED)
        return error

    def test_a_field_nested_1500_deep_is_answered_422_without_its_input(self) -> None:
        response = self.client.post("/sessions", content=('{"name": ' + _nested(1500) + "}").encode(), headers=JSON)

        if response.status_code == 400:  # the JSON reader went no deeper (3.11): its own answer
            self.assertEqual(response.json(), {"detail": "There was an error parsing the body"})
            return
        self.assertEqual(response.status_code, 422, response.text[:200])
        self.assertEqual(response.json(), {"detail": [NAME_REFUSED]})

    def test_a_field_nested_600_deep_around_each_leaf_is_answered_422_without_its_input(self) -> None:
        for name, leaf in LEAVES.items():
            with self.subTest(leaf=name):
                self.assertNotIn("input", self._refused(_nested(600, leaf)))

    def test_a_field_nested_800_deep_is_answered_without_its_input(self) -> None:
        # It was answered with its input, which held on every Python for a leaf of "x" alone: 800 is past the bound.
        self.assertNotIn("input", self._refused(_nested(800)))

    def test_an_input_at_the_bound_is_repeated_and_one_past_it_is_not(self) -> None:
        for name, leaf in LEAVES.items():
            with self.subTest(leaf=name):
                kept = self._refused(_nested(DEPTH_BOUND, leaf))
                self.assertEqual(_depth(kept["input"]), DEPTH_BOUND)
                self.assertNotIn("input", self._refused(_nested(DEPTH_BOUND + 1, leaf)))

    def test_a_shallow_input_is_repeated_as_before(self) -> None:
        self.assertEqual(self._refused(_nested(3))["input"], [[["x"]]])


class DeepRefusedToolCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "t", "version": "1"},
                "capabilities": {},
            },
        }
        init = self.client.post("/mcp", json=initialize)
        self.assertEqual(init.status_code, 200, init.text[:200])
        self.headers = {**JSON, "mcp-session-id": init.headers["mcp-session-id"], "mcp-protocol-version": "2025-11-25"}
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self.assertEqual(self.client.post("/mcp", json=initialized, headers=self.headers).status_code, 202)

    def _refused(self, name: str) -> dict:
        body = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":' + name + "}}"
        response = self.client.post("/mcp", content=body.encode(), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text[:200])
        error = response.json()["error"]
        self.assertEqual((error["code"], error["message"]), (-32602, "Invalid tools/call params"))
        (refused,) = error["data"]["errors"]
        self.assertEqual((refused["type"], refused["loc"]), ("string_type", ["name"]))
        return refused

    def test_a_name_nested_600_deep_around_each_leaf_is_answered_32602_without_its_input(self) -> None:
        for name, leaf in LEAVES.items():
            with self.subTest(leaf=name):
                self.assertNotIn("input", self._refused(_nested(600, leaf)))

    def test_a_shallow_input_is_repeated_as_before(self) -> None:
        self.assertEqual(self._refused(json.dumps([[1]]))["input"], [[1]])


if __name__ == "__main__":
    unittest.main()
