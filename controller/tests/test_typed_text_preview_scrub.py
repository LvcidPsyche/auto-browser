"""The typed text in an action's target reached the audit log and receipts unscrubbed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser.services.actions import BrowserActionService
from app.pii_scrub import PiiScrubber

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


async def test_type_stores_the_scrubbed_preview_in_the_action_target() -> None:
    manager = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(id="s1")),
        _run_action=AsyncMock(return_value={}),
        pii_scrubber=PiiScrubber(),
    )
    service = BrowserActionService(manager)

    await service.type("s1", text="alice@example.com", selector="#note")

    target = manager._run_action.await_args.args[2]
    assert target["text_preview"] == "[REDACTED]"


async def test_the_export_of_a_typed_email_fills_the_redaction() -> None:
    from app.playwright_export import _action_to_code

    manager = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(id="s1")),
        _run_action=AsyncMock(return_value={}),
        pii_scrubber=PiiScrubber(),
    )
    await BrowserActionService(manager).type("s1", text="alice@example.com", selector="#note")

    target = manager._run_action.await_args.args[2]

    assert "fill('[REDACTED]')" in _action_to_code("type", {"target": target})
    assert "alice" not in _action_to_code("type", {"target": target})
