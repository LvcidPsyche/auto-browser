"""The typed text in an action's target reached the audit log and receipts unscrubbed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.action_errors import BrowserActionError
from app.browser.services.actions import BrowserActionService
from app.browser_manager import BrowserManager
from app.pii_scrub import PiiScrubber
from tests.test_actions_verification import PAGE, URL, _session
from tests.test_browser_manager_actions import FakeContext, FakeLocator, FakePage, _settings

PREVIEW_CHARS = 80
SSN = "123-45-6789"


def _preview(text: str, scrubber: PiiScrubber | None) -> str:
    payload = BrowserActionService.text_target_payload(
        {"selector": "#note"},
        text,
        clear_first=True,
        sensitive=False,
        preview_chars=PREVIEW_CHARS,
        scrubber=scrubber,
    )
    return payload["text_preview"]


def test_a_typed_email_is_scrubbed_from_the_preview() -> None:
    assert _preview("alice@example.com", PiiScrubber()) == "[REDACTED]"


def test_a_plain_word_is_unchanged() -> None:
    assert _preview("playwright", PiiScrubber()) == "playwright"


def test_a_secret_straddling_the_preview_limit_is_caught() -> None:
    # The cut falls inside the number: cut first, and "123-4" no longer looks like an SSN and leaks.
    text = "x" * (PREVIEW_CHARS - 6) + " " + SSN + " tail"

    preview = _preview(text, PiiScrubber())

    assert "123" not in preview
    assert preview.endswith(" [REDA")


def test_scrubbing_off_leaves_the_preview_as_typed() -> None:
    assert _preview("alice@example.com", PiiScrubber(enabled=False)) == "alice@example.com"
    assert _preview("alice@example.com", None) == "alice@example.com"


def _typing_manager(tmp_path: Path, field: FakeLocator) -> tuple[BrowserManager, FakePage]:
    """The real type through the real pipeline, on a fake page whose ``#pw`` is ``field``; the records are mocks."""
    manager = BrowserManager(_settings(tmp_path))
    manager.audit.append = AsyncMock()
    manager._append_jsonl = AsyncMock()
    manager._persist_session = AsyncMock()
    manager._record_witness_receipt = AsyncMock()
    manager._settle = AsyncMock()
    manager._ensure_witness_remote_ready = AsyncMock()
    manager._check_bot_challenge = AsyncMock(return_value=None)
    manager._light_snapshot = AsyncMock(return_value={**PAGE, "tab_count": 1})
    manager._observation_payload = AsyncMock(return_value=PAGE)
    page = FakePage(URL)
    page.locators["#pw"] = field
    manager.sessions["s1"] = _session(tmp_path, page, FakeContext(page))
    return manager, page


def _recorded_targets(manager: BrowserManager) -> list[dict]:
    """The target of every record the type wrote: actions.jsonl, the audit log and the receipt."""
    lines = [call.args[1]["target"] for call in manager._append_jsonl.await_args_list]
    events = [call.kwargs["details"]["target"] for call in manager.audit.append.await_args_list]
    receipts = [call.kwargs["target"] for call in manager._record_witness_receipt.await_args_list]
    assert lines and events and receipts
    return [*lines, *events, *receipts]


async def _typed_email_target(tmp_path: Path) -> tuple[BrowserManager, dict]:
    """A type of an email into a field that reads not secret, through the real pipeline, and its result's target.
    The preview is withheld as the type begins; the one given back once the field reads not secret is scrubbed."""
    manager, _ = _typing_manager(tmp_path, FakeLocator("#pw", attributes={"type": "text", "name": "note"}))
    manager.pii_scrubber = PiiScrubber()
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", selector="#pw", text="alice@example.com")
    return manager, result["target"]


async def test_type_stores_the_scrubbed_preview_in_the_action_target(tmp_path: Path) -> None:
    manager, target = await _typed_email_target(tmp_path)

    for kept in (target, *_recorded_targets(manager)):
        assert kept["text_preview"] == "[REDACTED]", kept


async def test_a_field_whose_attributes_could_not_be_read_keeps_no_preview(tmp_path: Path) -> None:
    # Attributes that could not be read were taken to say "not secret": a password field that came in after the read
    # had timed out (ACTION_TIMEOUT_MS) and before the scroll had took the text, and its first 80 characters were
    # kept in the result, actions.jsonl, the audit log and the receipt. What is read now is the node the type holds
    # as the text begins (_TypedField), and a hold that cannot be taken says as little of it. With no clear first:
    # a type that clears its field finds it before the clear, and is refused where it is not found then.
    field = FakeLocator("#pw", attributes={"type": "password"})
    field.element_handle = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 1000ms exceeded."))
    manager, page = _typing_manager(tmp_path, field)

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", selector="#pw", text="hunter2-secret", clear_first=False)

    assert "".join(call.args[0] for call in page.keyboard.type.await_args_list) == "hunter2-secret"
    for target in (result["target"], *_recorded_targets(manager)):
        assert "text_preview" not in target and target.get("text_redacted") is True, target


async def test_a_type_into_a_field_that_never_came_records_no_preview(tmp_path: Path) -> None:
    # The type fails (the field is not there for the attribute read, nor for the scroll), and its failure record is
    # written from the same target: it kept the text that was to be typed.
    field = FakeLocator("#pw")
    field.get_attribute = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 15000ms exceeded."))
    field.scroll_into_view_if_needed = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 15000ms exceeded."))
    manager, page = _typing_manager(tmp_path, field)

    with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(BrowserActionError):
        await manager.type("s1", selector="#pw", text="hunter2-secret")

    page.keyboard.type.assert_not_awaited()
    for target in _recorded_targets(manager):
        assert "text_preview" not in target and target.get("text_redacted") is True, target


async def test_the_export_of_a_typed_email_fills_the_redaction(tmp_path: Path) -> None:
    from app.playwright_export import _action_to_code

    _, target = await _typed_email_target(tmp_path)

    assert "fill('[REDACTED]')" in _action_to_code("type", {"target": target})
    assert "alice" not in _action_to_code("type", {"target": target})
