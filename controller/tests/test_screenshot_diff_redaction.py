"""screenshot_diff stored raw pixels while every other screenshot path redacted PII."""

from __future__ import annotations

import asyncio
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.action_errors import SCREENSHOT_WITHHELD, BrowserActionError
from tests.test_allowed_hosts_rechecks import manager_for

EMAIL_BOX = {"x": 10, "y": 10, "width": 100, "height": 20}
INSIDE_BOX = (50, 20)
BLACK = (0, 0, 0)
OCR_WITH_EMAIL = {
    "available": True,
    "enabled": True,
    "text_excerpt": "ops@example.com",
    "blocks": [],
    "redaction_blocks": [{"text": "ops@example.com", "bbox": EMAIL_BOX, "confidence": 99, "line": (1, 1, 1)}],
}


def _black_inside_box(path: str | Path) -> bool:
    return Image.open(path).convert("RGB").getpixel(INSIDE_BOX) == BLACK


def _session(manager, tmp_path: Path) -> SimpleNamespace:
    session = SimpleNamespace(
        id="s1",
        page=SimpleNamespace(url="https://allowed.example/p", is_closed=lambda: False),
        lock=asyncio.Lock(),
        attached_pages=weakref.WeakSet(),
        context=SimpleNamespace(pages=[]),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
        artifact_dir=tmp_path,
        last_action=None,
        last_screenshot=None,
    )
    manager.get_session = AsyncMock(return_value=session)
    manager.sessions["s1"] = session
    return session


async def _capture(_session, label: str, **kwargs) -> dict:
    # white stands in for page pixels the scrubber must black out
    target = kwargs.get("target") or {"path": str(_session.artifact_dir / f"{label}.png"), "url": f"/a/{label}.png"}
    # PNG whatever the name ends in, as Playwright writes the type it is given: a capture still to be redacted goes to
    # a hidden name that is not .png (ACT-33).
    Image.new("RGB", (200, 60), "white").save(target["path"], format="PNG")
    return dict(target)


async def test_screenshot_diff_baseline_and_second_shot_are_redacted(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.allowed_hosts = None
    session = _session(manager, tmp_path)
    manager.artifacts.capture_screenshot = AsyncMock(side_effect=_capture)
    manager.pii_scrubber.screenshot_enabled = True
    manager.ocr.extract_from_image = AsyncMock(return_value=OCR_WITH_EMAIL)

    first = await manager.diagnostics.screenshot_diff("s1")
    assert first["baseline_captured"] is True
    baseline = tmp_path / "screenshot-baseline.png"
    assert _black_inside_box(baseline), "the saved baseline kept the unredacted pixels"

    session.last_screenshot = {"path": str(baseline), "url": first["baseline_url"]}
    await manager.diagnostics.screenshot_diff("s1")
    diff_b = sorted(tmp_path.glob("*diff-b*.png"))
    assert diff_b, "the second compare saved no diff-b capture"
    assert all(_black_inside_box(p) for p in diff_b), "diff-b kept the unredacted pixels"


async def test_screenshot_diff_reports_a_redaction_that_could_not_run(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.allowed_hosts = None
    _session(manager, tmp_path)
    manager.artifacts.capture_screenshot = AsyncMock(side_effect=_capture)
    manager.pii_scrubber.screenshot_enabled = True
    manager.pii_scrubber.screenshot_strict = False
    manager.ocr.extract_from_image = AsyncMock(return_value={"available": False, "error": "tesseract_not_found"})

    first = await manager.diagnostics.screenshot_diff("s1")

    assert first["screenshot_pii_scrub"] == {"applied": False, "reason": "tesseract_not_found"}


def _raw_prior(session: SimpleNamespace, tmp_path: Path) -> Path:
    prior = tmp_path / "prior.png"
    Image.new("RGB", (200, 60), "white").save(prior)
    session.last_screenshot = {"path": str(prior), "url": "/a/prior.png"}
    return prior


async def test_screenshot_diff_redacts_an_unredacted_prior_before_comparing(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.allowed_hosts = None
    session = _session(manager, tmp_path)
    _raw_prior(session, tmp_path)
    manager.artifacts.capture_screenshot = AsyncMock(side_effect=_capture)
    manager.pii_scrubber.screenshot_enabled = True
    manager.ocr.extract_from_image = AsyncMock(return_value=OCR_WITH_EMAIL)

    result = await manager.diagnostics.screenshot_diff("s1")

    assert result["changed_pixels"] == 0, "the raw prior was diffed against the redacted shot"
    assert not result["a_url"].endswith("prior.png"), "the diff points at the raw prior"
    diff = Image.open(result["diff_path"]).convert("RGB")
    assert diff.getpixel(INSIDE_BOX) == BLACK


async def test_strict_mode_refuses_a_diff_whose_prior_cannot_be_scrubbed(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.allowed_hosts = None
    session = _session(manager, tmp_path)
    _raw_prior(session, tmp_path)
    manager.artifacts.capture_screenshot = AsyncMock(side_effect=_capture)
    manager.pii_scrubber.screenshot_enabled = True
    manager.pii_scrubber.screenshot_strict = True
    # the new shot scrubs; the prior's OCR fails
    manager.ocr.extract_from_image = AsyncMock(
        side_effect=[OCR_WITH_EMAIL, {"available": False, "error": "ocr_extraction_failed"}]
    )

    with pytest.raises(BrowserActionError) as raised:
        await manager.diagnostics.screenshot_diff("s1")

    assert raised.value.code == SCREENSHOT_WITHHELD


async def test_strict_mode_refuses_a_diff_whose_new_shot_cannot_be_scrubbed(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.allowed_hosts = None
    _session(manager, tmp_path)
    manager.artifacts.capture_screenshot = AsyncMock(side_effect=_capture)
    manager.pii_scrubber.screenshot_enabled = True
    manager.pii_scrubber.screenshot_strict = True
    manager.ocr.extract_from_image = AsyncMock(return_value={"available": False, "error": "tesseract_not_found"})

    with pytest.raises(BrowserActionError) as raised:
        await manager.diagnostics.screenshot_diff("s1")

    assert raised.value.code == SCREENSHOT_WITHHELD
