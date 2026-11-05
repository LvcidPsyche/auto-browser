"""A tool result always goes out as valid JSON, and a result that cannot be encoded is said to be one.

`json.dumps` wrote NaN as the bare word NaN (invalid JSON) with isError false, and a value it could not
serialize (a datetime, an object) became "Tool execution failed" after the tool's side effect had already run.
Round 4: only known types are sent as their text; any other object (a dataclass whose repr holds a secret) is
refused, and an error's details are encoded as strictly as a result.
"""

import dataclasses
import datetime
import decimal
import enum
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.action_errors import BrowserActionError
from app.config import Settings
from app.models import McpToolCallRequest
from app.tool_gateway.gateway import McpToolGateway

SECRET = "JBSWY3DPEHPK3PXP"


class Colour(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class SessionLike:
    id: str
    totp_secret: str


def _strict_loads(text: str):
    def refuse(constant: str):
        raise ValueError(f"invalid JSON constant {constant}")

    return json.loads(text, parse_constant=refuse)


class ResultEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, result=None, *, error: Exception | None = None):
        self.manager = SimpleNamespace(
            settings=Settings(_env_file=None),
            ensure_session_accessible=AsyncMock(),
            close_tab=AsyncMock(return_value=result, side_effect=error),
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

    async def test_known_types_become_their_text_and_an_enum_its_value(self) -> None:
        moment = datetime.datetime(2026, 1, 1, 12, 30)
        ident = uuid.UUID(int=1)
        known = {
            "datetime": moment,
            "date": moment.date(),
            "time": moment.time(),
            "path": Path("artifacts") / "shot.png",
            "uuid": ident,
            "decimal": decimal.Decimal("1.50"),
            "enum": Colour.RED,
            "keys": {2: "a", True: "b", None: "c"},
        }

        response = await self._call(known)

        self.assertFalse(response.isError)
        parsed = _strict_loads(response.content[0].text)
        self.assertEqual(
            parsed,
            {
                "datetime": str(moment),
                "date": str(moment.date()),
                "time": str(moment.time()),
                "path": str(Path("artifacts") / "shot.png"),
                "uuid": str(ident),
                "decimal": "1.50",
                "enum": "red",
                "keys": {"2": "a", "true": "b", "null": "c"},  # as json.dumps spells the keys
            },
        )

    async def test_an_object_of_any_other_type_is_refused_not_sent_as_its_text(self) -> None:
        opaque = object()

        response = await self._call({"closed_index": 1, "thing": opaque})

        self.assertTrue(response.isError)
        self.assertIn("could not be encoded", response.content[0].text)
        self.assertNotIn(str(opaque), str(response.model_dump()))

    async def test_a_dataclass_is_refused_so_its_repr_does_not_leak_a_secret(self) -> None:
        response = await self._call({"session": SessionLike(id="s1", totp_secret=SECRET)})

        self.assertTrue(response.isError)
        self.assertIn("ran, but its result could not be encoded", response.content[0].text)
        self.assertNotIn(SECRET, str(response.model_dump()))

    async def test_a_nan_in_a_browser_action_errors_details_is_valid_json(self) -> None:
        error = BrowserActionError("click failed", details={"snapshot": {"scroll_ratio": float("nan")}})

        response = await self._call(error=error)

        self.assertTrue(response.isError)
        parsed = _strict_loads(response.content[0].text)
        self.assertEqual((parsed["error"], parsed["snapshot"]), ("click failed", {"scroll_ratio": None}))
        self.assertEqual(response.structuredContent, parsed)

    async def test_an_unencodable_value_in_an_errors_details_is_dropped_and_the_rest_kept(self) -> None:
        details = {"snapshot": {"url": "https://example.com"}, "session": SessionLike(id="s1", totp_secret=SECRET)}
        error = BrowserActionError("click failed", code="browser_action_failed", details=details)

        response = await self._call(error=error)

        self.assertTrue(response.isError)
        parsed = _strict_loads(response.content[0].text)
        self.assertEqual(
            (parsed["code"], parsed["snapshot"]), ("browser_action_failed", {"url": "https://example.com"})
        )
        self.assertNotIn("session", parsed)
        self.assertNotIn(SECRET, str(response.model_dump()))

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
