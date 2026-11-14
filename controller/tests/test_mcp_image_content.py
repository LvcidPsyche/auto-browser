"""browser.screenshot and observe's fast preset show the page to the model.

Both returned only a path on the controller's disk and a URL on the
controller, which a model behind an MCP client cannot open, so the
screenshot-only views gave a vision model nothing to look at. The screenshot
now also arrives as MCP image content, after the JSON text block that clients
read as the result.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from app.models import McpToolCallRequest, McpToolCallResponse
from app.tool_gateway import McpToolGateway

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"not-really-pixels" * 8


class InlineScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.artifact_root = self.root / "artifacts"
        self.shot = self.artifact_root / "sess-1" / "20260101T000000Z-manual.png"
        self.shot.parent.mkdir(parents=True)
        self.shot.write_bytes(PNG_BYTES)
        self.manager = SimpleNamespace(
            ensure_session_accessible=AsyncMock(),
            settings=SimpleNamespace(artifact_root=str(self.artifact_root)),
            capture_screenshot=AsyncMock(side_effect=lambda *a, **k: self._shot_result()),
            observe=AsyncMock(side_effect=lambda *a, preset=None, **k: self._observation(preset or "normal")),
            require_governed_approval=AsyncMock(return_value=None),
        )
        self.gateway = McpToolGateway(manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

    def _shot_result(self, path: Path | None = None) -> dict[str, Any]:
        return {
            "url": "https://example.com",
            "screenshot_path": str(path or self.shot),
            "screenshot_url": "/artifacts/sess-1/20260101T000000Z-manual.png",
            "takeover_url": "http://127.0.0.1:6080/vnc.html",
        }

    def _observation(self, preset: str) -> dict[str, Any]:
        return {**self._shot_result(), "title": "Example", "interactables": [], "preset": preset}

    async def _call(self, name: str, **arguments: Any) -> McpToolCallResponse:
        response = await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))
        self.assertFalse(response.isError, response.content[0].text)
        self.assertEqual(response.content[0].type, "text")
        self.assertEqual(json.loads(response.content[0].text), response.structuredContent)
        return response

    async def test_screenshot_returns_the_image_after_the_json(self) -> None:
        response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual(len(response.content), 2)
        image = response.content[1]
        self.assertEqual(image.type, "image")
        self.assertEqual(image.mimeType, "image/png")
        self.assertEqual(base64.b64decode(image.data), PNG_BYTES)
        self.assertEqual(response.structuredContent["screenshot_url"], "/artifacts/sess-1/20260101T000000Z-manual.png")

    async def test_a_screenshot_path_given_as_a_path_object_still_gets_the_image(self) -> None:
        # json_safe turns it into its text, and the image is decided from that text (as at 1.17.0).
        self.manager.capture_screenshot = AsyncMock(
            return_value={**self._shot_result(), "screenshot_path": PurePath(self.shot)}
        )

        response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual([block.type for block in response.content], ["text", "image"])
        self.assertEqual(base64.b64decode(response.content[1].data), PNG_BYTES)
        self.assertEqual(response.structuredContent["screenshot_path"], str(self.shot))

    async def test_only_the_fast_preset_inlines_the_screenshot(self) -> None:
        for preset, expected_blocks in (("fast", ["text", "image"]), ("normal", ["text"]), ("rich", ["text"])):
            with self.subTest(preset=preset):
                response = await self._call("browser.observe", session_id="sess-1", preset=preset)
                self.assertEqual([block.type for block in response.content], expected_blocks)

    async def test_a_path_outside_the_artifact_root_is_never_read(self) -> None:
        secret = self.root / "secret.png"
        secret.write_bytes(b"\x89PNG not an artifact")
        self.manager.capture_screenshot = AsyncMock(return_value=self._shot_result(secret))

        response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual([block.type for block in response.content], ["text"])

    async def test_traversal_out_of_the_artifact_root_is_never_read(self) -> None:
        (self.root / "secret.png").write_bytes(b"\x89PNG not an artifact")
        escaped = self.artifact_root / "sess-1" / ".." / ".." / "secret.png"
        self.manager.capture_screenshot = AsyncMock(return_value=self._shot_result(escaped))

        response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual([block.type for block in response.content], ["text"])

    async def test_oversized_missing_or_non_image_files_stay_text_only(self) -> None:
        other = self.artifact_root / "sess-1" / "trace.zip"
        other.write_bytes(b"PK")
        cases = {
            "missing": self.artifact_root / "sess-1" / "gone.png",
            "not an image": other,
        }
        for label, path in cases.items():
            with self.subTest(label):
                self.manager.capture_screenshot = AsyncMock(return_value=self._shot_result(path))
                response = await self._call("browser.screenshot", session_id="sess-1")
                self.assertEqual([block.type for block in response.content], ["text"])

        self.manager.capture_screenshot = AsyncMock(return_value=self._shot_result())
        with patch("app.tool_gateway.gateway._INLINE_IMAGE_MAX_BYTES", len(PNG_BYTES) - 1):
            response = await self._call("browser.screenshot", session_id="sess-1")
        self.assertEqual([block.type for block in response.content], ["text"])

    async def _omitted(self, path: Path, *, patch_max: int | None = None) -> McpToolCallResponse:
        self.manager.capture_screenshot = AsyncMock(return_value=self._shot_result(path))
        if patch_max is None:
            return await self._call("browser.screenshot", session_id="sess-1")
        with patch("app.tool_gateway.gateway._INLINE_IMAGE_MAX_BYTES", patch_max):
            return await self._call("browser.screenshot", session_id="sess-1")

    async def test_a_withheld_image_names_why_in_both_the_text_and_the_structured_result(self) -> None:
        other = self.artifact_root / "sess-1" / "trace.zip"
        other.write_bytes(b"PK")
        outside = self.root / "secret.png"
        outside.write_bytes(b"\x89PNG not an artifact")
        cases = (
            ("too_large", self.shot, len(PNG_BYTES) - 1),
            ("unreadable", self.artifact_root / "sess-1" / "gone.png", None),
            ("unsupported_type", other, None),
            ("outside_artifact_root", outside, None),
        )
        for reason, path, patch_max in cases:
            with self.subTest(reason=reason):
                response = await self._omitted(path, patch_max=patch_max)

                self.assertEqual([block.type for block in response.content], ["text"])
                self.assertEqual(response.structuredContent["image_omitted"], reason)
                self.assertEqual(json.loads(response.content[0].text)["image_omitted"], reason)

    async def test_a_controller_with_no_artifact_root_withholds_the_image_and_says_so(self) -> None:
        self.manager.settings.artifact_root = None

        response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual([block.type for block in response.content], ["text"])
        self.assertEqual(response.structuredContent["image_omitted"], "no_artifact_root")

    async def test_a_screenshot_that_cannot_be_read_is_an_answer_not_a_failure_to_encode(self) -> None:
        missing = self.artifact_root / "sess-1" / "gone.png"
        self.manager.capture_screenshot = AsyncMock(return_value=self._shot_result(missing))
        with patch("app.tool_gateway.gateway._read_inline_image", side_effect=OSError("C:/secret/path denied")):
            response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual(response.structuredContent["image_omitted"], "unreadable")
        self.assertNotIn("could not be encoded", response.content[0].text)
        self.assertNotIn("secret", response.content[0].text)

    async def test_a_path_a_filesystem_cannot_hold_reads_as_unreadable(self) -> None:
        # Refused by the path layer itself, not a stat failure: raised before the bytes are read at all.
        response = await self._omitted(self.artifact_root / "sess-1" / "bad\0name.png")

        self.assertEqual(response.structuredContent["image_omitted"], "unreadable")

    async def test_a_result_with_its_image_has_no_omission_key(self) -> None:
        response = await self._call("browser.screenshot", session_id="sess-1")

        self.assertEqual([block.type for block in response.content], ["text", "image"])
        self.assertNotIn("image_omitted", response.structuredContent)

    async def test_a_result_not_meant_to_carry_an_image_has_no_omission_key(self) -> None:
        response = await self._call("browser.observe", session_id="sess-1", preset="normal")

        self.assertEqual([block.type for block in response.content], ["text"])
        self.assertNotIn("image_omitted", response.structuredContent)

    async def test_image_block_serializes_as_mcp_image_content(self) -> None:
        response = await self._call("browser.screenshot", session_id="sess-1")

        wire = response.model_dump(exclude_none=True, by_alias=True)

        self.assertEqual(
            wire["content"][1],
            {"type": "image", "data": base64.b64encode(PNG_BYTES).decode("ascii"), "mimeType": "image/png"},
        )
        self.assertEqual(McpToolCallResponse.model_validate(wire).content[1].type, "image")


if __name__ == "__main__":
    unittest.main()
