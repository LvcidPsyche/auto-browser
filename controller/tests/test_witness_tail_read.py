"""A chain past 64 KiB keeps accepting receipts when its text is not ASCII.

The chain is UTF-8 and pydantic writes non-ASCII text as it is. `read_tail_line`
used to seek a byte offset (size - 64 KiB) on a text handle and decode the window
strictly, so once the window began inside a multi-byte character every append
raised `UnicodeDecodeError`, and every later action ran and then reported a
failure. Which alignment a chain lands on depends on its bytes, so these tests
shift it with an ASCII pad after the text.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.utils import UTC
from app.witness import WitnessRecorder
from app.witness_anchor import TAIL_READ_BYTES, read_tail_line
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings

OPERATOR = {"id": "alice", "source": "header"}


async def _record(recorder: WitnessRecorder, scope: str, *, text: str, action: str = "click", tail_pad: int = 0):
    return await recorder.record(
        scope,
        profile="normal",
        event_type="browser_action",
        status="ok",
        action=action,
        action_class="write",
        operator=OPERATOR,
        metadata={"page_text": text, "z": "a" * tail_pad},
    )


class TailReadRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_after_a_64kib_chain_of_3_byte_characters_succeeds_at_every_alignment(self) -> None:
        # A 3-byte character leaves three possible window starts; the pad walks them.
        for pad in range(3):
            with self.subTest(pad=pad), tempfile.TemporaryDirectory() as tempdir:
                recorder = WitnessRecorder(Path(tempdir))
                await recorder.startup()
                first = await _record(recorder, "s", text="€" * 33000, tail_pad=pad)
                self.assertGreater((Path(tempdir) / "s.jsonl").stat().st_size, TAIL_READ_BYTES)

                second = await _record(recorder, "s", text="next", action="second")

                self.assertEqual(second.chain_prev_hash, first.chain_hash)
                result = await recorder.verify("s")
                self.assertTrue(result["valid"], result["reason"])
                self.assertEqual(result["receipt_count"], 2)

    async def test_every_append_succeeds_as_a_chain_of_2_byte_characters_grows_past_64kib(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            recorder = WitnessRecorder(Path(tempdir))
            await recorder.startup()
            path = Path(tempdir) / "s.jsonl"
            for index in range(40):
                await _record(recorder, "s", text="é" * 2000, action=f"a{index}", tail_pad=index % 2)
            self.assertGreater(path.stat().st_size, TAIL_READ_BYTES)

            result = await recorder.verify("s")
            self.assertTrue(result["valid"], result["reason"])
            self.assertEqual(result["receipt_count"], 40)

    async def test_ascii_chain_past_64kib_still_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            recorder = WitnessRecorder(Path(tempdir))
            await recorder.startup()
            await _record(recorder, "s", text="a" * 100000)
            second = await _record(recorder, "s", text="next", action="second")
            self.assertIsNotNone(second.chain_prev_hash)

    async def test_a_last_line_longer_than_the_window_is_read_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            recorder = WitnessRecorder(Path(tempdir))
            await recorder.startup()
            await _record(recorder, "s", text="x")
            last = await _record(recorder, "s", text="日本語" * 30000, action="long")

            with open(Path(tempdir) / "s.jsonl", "a+", encoding="utf-8") as handle:
                tail = read_tail_line(handle)

            self.assertIn(last.chain_hash, tail)
            self.assertIn("日本語", tail)


class ReadTailLineTests(unittest.TestCase):
    def _read(self, data: bytes) -> str | None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "chain.jsonl"
            path.write_bytes(data)
            with open(path, "a+", encoding="utf-8") as handle:
                return read_tail_line(handle)

    def test_an_empty_file_has_no_tail_line(self) -> None:
        self.assertIsNone(self._read(b""))

    def test_blank_lines_are_skipped_and_crlf_is_stripped(self) -> None:
        self.assertEqual(self._read(b'{"a":1}\r\n{"a":2}\r\n\r\n\n'), '{"a":2}')

    def test_a_last_line_holding_a_line_separator_is_returned_whole(self) -> None:
        last = '{"t":"a b c\u0085d"}'
        self.assertEqual(self._read(('{"t":1}\n' + last + "\n").encode("utf-8")), last)

    def test_a_window_starting_inside_a_character_is_not_decoded(self) -> None:
        last = '{"t":"' + "é" * 10 + '"}'
        # The window opens on the continuation byte of the first line's last "€".
        first = '{"t":"' + "€" * 30000 + '"}'
        data = (first + "\n" + last + "\n").encode("utf-8")
        offset = len(data) - TAIL_READ_BYTES
        self.assertEqual(data[offset] & 0xC0, 0x80, "fixture must open the window mid-character")
        self.assertEqual(self._read(data), last)


class TailReadManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        settings = _settings(self.root)
        settings.witness_enabled = True
        self.manager = BrowserManager(settings)
        await self.manager.witness.startup()
        self.manager.audit.append = AsyncMock()
        self.manager._append_jsonl = AsyncMock()
        self.manager._persist_session = AsyncMock()
        page = FakePage()
        context = FakeContext(page)
        artifact_dir = self.root / "artifacts" / "session-1"
        artifact_dir.mkdir(parents=True)
        auth = self.root / "auth" / "session-1"
        upload = self.root / "uploads" / "session-1"
        auth.mkdir(parents=True)
        upload.mkdir(parents=True)
        self.session = BrowserSession(
            id="session-1",
            name="fixture",
            created_at=datetime.now(UTC),
            context=context,
            page=page,
            artifact_dir=artifact_dir,
            auth_dir=auth,
            upload_dir=upload,
            takeover_url="http://localhost:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.sessions["session-1"] = self.session
        self.manager._light_snapshot = AsyncMock(return_value={"url": "https://example.com", "title": "t"})
        self.manager._settle = AsyncMock()
        self.manager._ensure_witness_remote_ready = AsyncMock()
        self.manager._check_bot_challenge = AsyncMock(return_value=None)

    async def test_actions_on_a_non_ascii_page_keep_working_after_the_chain_passes_64kib(self) -> None:
        # The page's text is copied into each receipt's evidence.after, so a
        # non-English page grows the chain with non-ASCII.
        self.manager._observation_payload = AsyncMock(
            return_value={"url": "https://example.com", "title": "t", "text_excerpt": "€" * 3000}
        )
        ran: list[int] = []

        async def click() -> None:
            ran.append(1)

        for _ in range(30):
            await BrowserManager._run_action(self.manager, self.session, "click", {"selector": "#b"}, click)

        chain = self.root / "witness" / "session-1.jsonl"
        self.assertGreater(chain.stat().st_size, TAIL_READ_BYTES)
        self.assertEqual(len(ran), 30)
        result = await self.manager.witness.verify("session-1")
        self.assertTrue(result["valid"], result["reason"])

    async def test_a_page_title_with_a_line_separator_does_not_stop_later_receipts(self) -> None:
        # U+2028 is raw in JSON; a reader that split lines on it cut the receipt in two.
        payload = {"url": "https://example.com", "title": "Sale\u2028ends today", "text_excerpt": "hi"}
        self.manager._light_snapshot = AsyncMock(return_value=dict(payload))
        self.manager._observation_payload = AsyncMock(return_value=dict(payload))
        ran: list[int] = []

        async def click() -> None:
            ran.append(1)

        for _ in range(3):
            await BrowserManager._run_action(self.manager, self.session, "click", {"selector": "#b"}, click)

        result = await self.manager.witness.verify("session-1")
        self.assertEqual(len(ran), 3)
        self.assertEqual(result["receipt_count"], 3)
        self.assertTrue(result["valid"], result["reason"])

    async def test_lone_surrogates_in_a_pages_title_neither_fail_the_action_nor_cost_its_receipt(self) -> None:
        """SG-2 (Codex M1): a string can hold a lone surrogate ('Sale\\ud800today'), which UTF-8 cannot encode: page
        text read through a driver that hands it over (Chromium replaces one with U+FFFD; tool arguments cannot carry
        one, as pydantic's str refuses it, string_unicode). The action ran, then failed writing actions.jsonl and its
        receipt, as ACT-01 did. Normal, malformed, malformed, recovered: four receipts, and no error."""
        del self.manager._append_jsonl  # the real actions.jsonl writer
        titles = ["Sale today", "Sale\ud800today", "Sale\udc00today", "Sale today"]
        written = ["Sale today", "Sale�today", "Sale�today", "Sale today"]
        ran: list[int] = []

        async def click() -> None:
            ran.append(1)

        for title in titles:
            payload = {"url": "https://example.com", "title": title, "text_excerpt": "hi"}
            self.manager._light_snapshot = AsyncMock(return_value=dict(payload))
            self.manager._observation_payload = AsyncMock(return_value=dict(payload))
            await BrowserManager._run_action(self.manager, self.session, "click", {"selector": "#b"}, click)

        result = await self.manager.witness.verify("session-1")
        self.assertEqual(len(ran), 4)
        self.assertEqual(result["receipt_count"], 4)
        self.assertTrue(result["valid"], result["reason"])
        receipts = list(reversed(await self.manager.witness.list("session-1", limit=10)))
        self.assertEqual([receipt.evidence.before["title"] for receipt in receipts], written)
        lines = (self.session.artifact_dir / "actions.jsonl").read_text(encoding="utf-8").split("\n")[:-1]
        self.assertEqual([json.loads(line)["before"]["title"] for line in lines], written)

    async def test_a_receipt_recorded_outside_an_action_survives_a_lone_surrogate(self) -> None:
        """The auth-profile receipts are recorded straight to the store, with the profile's name in their target."""
        recorder = self.manager.witness

        await recorder.record(
            "auth-profiles",
            profile="normal",
            event_type="auth_profile",
            status="ok",
            action="export_auth_profile",
            action_class="auth",
            operator={"id": "alice", "source": "header"},
            target={"profile_name": "shop\udfffmain"},
        )

        result = await recorder.verify("auth-profiles")
        self.assertTrue(result["valid"], result["reason"])
        receipts = await recorder.list("auth-profiles", limit=1)
        self.assertEqual(receipts[0].target, {"profile_name": "shop�main"})
