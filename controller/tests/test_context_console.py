"""The console listener on a session's browser context (diagnostics.watch_context). The context carries every page's
console messages, and the controller keeps one only as the output of a page the session has attached. These use
fakes, so each message's page is exactly the one the test names."""

from __future__ import annotations

import weakref
from types import SimpleNamespace

from app.browser.services.diagnostics import BrowserDiagnosticsService
from tests.test_active_tab_close import FakePage, fake_manager


class FakeContext:
    """Records its listeners; ``log`` delivers a console message to them, as Playwright's context does."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def log(self, text: str, page) -> None:
        message = SimpleNamespace(type="log", text=text, location={}, page=page)
        for handler in self.handlers.get("console", []):
            handler(message)


def test_a_console_message_is_kept_only_as_the_output_of_a_page_the_session_has_attached() -> None:
    """A message of a page the session has not attached is not the session's to keep, and one with no page (a service
    worker's) has no page to be judged by: neither is kept. A message of an attached page is."""
    service = BrowserDiagnosticsService(fake_manager(), None, None)  # type: ignore[arg-type]
    attached = FakePage()
    session = SimpleNamespace(id="s1", page=attached, attached_pages=weakref.WeakSet(), console_messages=[])
    context = FakeContext()
    service.watch_context(context, session)
    service.attach_page_listeners(attached, session)

    context.log("LINE-OF-AN-ATTACHED-PAGE", attached)
    context.log("LINE-OF-AN-UNATTACHED-PAGE", FakePage())
    context.log("LINE-OF-NO-PAGE", None)

    assert [message["text"] for message in session.console_messages] == ["LINE-OF-AN-ATTACHED-PAGE"]
