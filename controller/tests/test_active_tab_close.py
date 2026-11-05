from __future__ import annotations

import asyncio
import unittest
import weakref
from types import SimpleNamespace

from app.browser.page_provenance import PageProvenance
from app.browser.services.diagnostics import BrowserDiagnosticsService


class FakePage:
    def __init__(self) -> None:
        self.url = "https://allowed.example/"
        self.handlers: dict[str, list] = {}
        self.closed = False

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def is_closed(self) -> bool:
        return self.closed

    def close_itself(self) -> None:
        self.closed = True
        for handler in self.handlers.get("close", []):
            handler(self)


def fake_manager(refused: tuple[FakePage, ...] = ()) -> SimpleNamespace:
    """What the close listener reads of the manager: provenance, and whether ALLOWED_HOSTS refuses a URL or a page."""
    return SimpleNamespace(
        page_provenance=PageProvenance(),
        _url_off_list=lambda _url: False,
        _page_refused=lambda page: page in refused,
    )


class ActiveTabCloseTests(unittest.TestCase):
    @staticmethod
    def _session(*pages: FakePage) -> SimpleNamespace:
        return SimpleNamespace(
            id="s1",
            page=pages[0],
            context=SimpleNamespace(pages=list(pages)),
            attached_pages=weakref.WeakSet(),
            console_messages=[],
            page_errors=[],
            request_failures=[],
        )

    def test_session_moves_to_another_tab_when_its_active_tab_closes_itself(self) -> None:
        """A page calling window.close() left session.page on a closed page,
        so every later action failed while other tabs were open."""
        service = BrowserDiagnosticsService(fake_manager(), None, None)  # type: ignore[arg-type]
        opener, popup = FakePage(), FakePage()
        session = self._session(opener, popup)
        for page in (opener, popup):
            service.attach_page_listeners(page, session)
        session.page = popup

        popup.close_itself()

        self.assertIs(session.page, opener)

    def test_closing_a_background_tab_keeps_the_active_one(self) -> None:
        service = BrowserDiagnosticsService(fake_manager(), None, None)  # type: ignore[arg-type]
        active, background = FakePage(), FakePage()
        session = self._session(active, background)
        for page in (active, background):
            service.attach_page_listeners(page, session)

        background.close_itself()

        self.assertIs(session.page, active)


class RefusedTabsAreNotChosenTests(unittest.TestCase):
    """NAV-09: when the active tab closes, the session moves to a tab ALLOWED_HOSTS allows, if any is left."""

    def test_a_refused_last_tab_is_passed_over(self) -> None:
        opener, popup, refused = FakePage(), FakePage(), FakePage()
        service = BrowserDiagnosticsService(fake_manager((refused,)), None, None)  # type: ignore[arg-type]
        session = ActiveTabCloseTests._session(opener, popup, refused)
        for page in (opener, popup, refused):
            service.attach_page_listeners(page, session)
        session.page = popup

        popup.close_itself()

        self.assertIs(session.page, opener)

    def test_when_every_remaining_tab_is_refused_the_old_choice_stands(self) -> None:
        popup, first, second = FakePage(), FakePage(), FakePage()
        service = BrowserDiagnosticsService(fake_manager((first, second)), None, None)  # type: ignore[arg-type]
        session = ActiveTabCloseTests._session(popup, first, second)
        for page in (popup, first, second):
            service.attach_page_listeners(page, session)
        session.page = popup

        popup.close_itself()

        self.assertIs(session.page, second)


class ClosedWhileHeldTests(unittest.TestCase):
    """The close record of a held popup (_closed_while_held) keeps what it logged for a clean verdict. It holds
    only a popup held for nothing but its pending birth verdict: an off-list commit taints the popup, so its
    output is never kept for a later clean verdict. That is why the record needs no URL check of its own."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        manager = fake_manager()
        # A tainted page counts for its session, so its close also asks whether the session is watched.
        manager.window_births = SimpleNamespace(active_for=lambda _session: False)
        self.service = BrowserDiagnosticsService(manager, None, None)  # type: ignore[arg-type]
        self.provenance = manager.page_provenance
        self.opener, self.popup = FakePage(), FakePage()
        self.session = ActiveTabCloseTests._session(self.opener, self.popup)
        for page in (self.opener, self.popup):
            self.service.attach_page_listeners(page, self.session)
        # Reported and held for its birth verdict, which is still being judged.
        self.provenance.opener_lookup_started(self.popup)
        self.service._birth_judgements[self.popup] = self.loop.create_future()

    def test_a_popup_held_only_for_its_verdict_is_recorded_when_it_closes(self) -> None:
        self.assertTrue(self.provenance.held_for_verdict(self.popup))

        self.popup.close_itself()

        self.assertIn(self.popup, self.service._closed_while_held)

    def test_a_held_popup_that_commits_an_off_list_url_is_never_recorded(self) -> None:
        self.popup.url = "https://refused.example/"
        self.provenance.committed(self.popup, self.popup.url, url_refused=True)
        self.assertFalse(self.provenance.held_for_verdict(self.popup), "an off-list commit taints the popup")

        self.popup.close_itself()

        self.assertNotIn(self.popup, self.service._closed_while_held)


if __name__ == "__main__":
    unittest.main()
