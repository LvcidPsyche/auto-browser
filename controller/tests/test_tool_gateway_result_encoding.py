"""A tool result always goes out as valid JSON, and a result that cannot be encoded is said to be one.

`json.dumps` wrote NaN as the bare word NaN (invalid JSON) with isError false, and a value it could not
serialize (a datetime, an object) became "Tool execution failed" after the tool's side effect had already run.
"""

import datetime
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import Settings
from app.models import McpToolCallRequest
from app.tool_gateway.gateway import McpToolGateway


def _strict_loads(text: str):
    def refuse(constant: str):
        raise ValueError(f"invalid JSON constant {constant}")

    return json.loads(text, parse_constant=refuse)


class ResultEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, result):
        self.manager = SimpleNamespace(
            settings=Settings(_env_file=None),
            ensure_session_accessible=AsyncMock(),
            close_tab=AsyncMock(return_value=result),
        )
        gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )
        response = await gateway.call_tool(
            McpToolCallRequest(name="browser.close_tab", arguments={"session_id": "session-1", "index": 1})
        )
        self.manager.close_tab.assert_awaited_once()  # the side effect ran, once
        return response

    async def test_nan_and_infinity_become_null(self) -> None:
        response = await self._call({"closed_index": float("nan"), "ratio": float("inf"), "ok": [1.5, float("-inf")]})

        self.assertFalse(response.isError)
        parsed = _strict_loads(response.content[0].text)
        self.assertEqual(parsed, {"closed_index": None, "ratio": None, "ok": [1.5, None]})
        self.assertEqual(response.structuredContent, parsed)

    async def test_a_datetime_and_an_object_become_strings(self) -> None:
        opaque = object()
        response = await self._call({"closed_index": datetime.datetime(2026, 1, 1), "thing": opaque, "keys": {1: "a"}})

        self.assertFalse(response.isError)
        parsed = _strict_loads(response.content[0].text)
        self.assertEqual(parsed["closed_index"], str(datetime.datetime(2026, 1, 1)))
        self.assertEqual(parsed["thing"], str(opaque))
        self.assertEqual(parsed["keys"], {"1": "a"})

    async def test_a_result_that_cannot_be_encoded_says_the_action_ran(self) -> None:
        loop: dict = {}
        loop["self"] = loop

        response = await self._call(loop)

        self.assertTrue(response.isError)
        self.assertIn("ran", response.content[0].text)
        self.assertIn("could not be encoded", response.content[0].text)

    async def test_an_ordinary_result_is_unchanged(self) -> None:
        response = await self._call({"closed_index": 1, "tabs": [{"index": 0, "active": True, "title": "é"}]})

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["tabs"][0]["title"], "é")
        self.assertIn("é", response.content[0].text)


if __name__ == "__main__":
    unittest.main()
