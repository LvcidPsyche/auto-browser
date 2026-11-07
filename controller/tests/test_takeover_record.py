"""The takeover URL is for the person asked to take over, not for actions.jsonl, which /artifacts serves (NAV-14)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, build_manager

URL = "https://takeover.example.org:16181/vnc.html?token=LINK-MARKER"


class TakeoverRecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        with acting_as(ALICE):
            self.session_id = (await self.manager.create_session(name="alice"))["id"]
        self.session = self.manager.sessions[self.session_id]
        self.session.takeover_url = URL

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def request(self) -> dict:
        with acting_as(ALICE):
            return await self.manager.request_human_takeover(self.session_id, "log in with the code from my phone")

    def lines(self) -> list[dict]:
        text = (self.session.artifact_dir / "actions.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    async def test_the_response_still_carries_the_url(self) -> None:
        result = await self.request()

        self.assertIn("LINK-MARKER", result["takeover_url"])

    async def test_actions_jsonl_does_not(self) -> None:
        await self.request()

        text = (self.session.artifact_dir / "actions.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("LINK-MARKER", text)
        line = [item for item in self.lines() if item["action"] == "request_human_takeover"][0]
        self.assertNotIn("takeover_url", line)
        self.assertNotIn("remote_access", line)
        self.assertNotIn("takeover_url", line["session"])
        self.assertNotIn("remote_access", line["session"])

    async def test_the_record_keeps_the_reason_and_the_session(self) -> None:
        await self.request()

        line = [item for item in self.lines() if item["action"] == "request_human_takeover"][0]
        self.assertEqual(line["reason"], "log in with the code from my phone")
        self.assertEqual(line["session"]["id"], self.session_id)
        self.assertIn("timestamp", line)


if __name__ == "__main__":
    unittest.main()
