"""Refused hops only a browser context saw are kept per context (round 4c, item 7, PROV-3).

A popup's first navigation reaches only the context's listeners: its page is not reported yet. The hops
were kept in one list on the manager, and a page the context reported dropped every kept hop that
resolved to some other page, so when two popups opened together, the first one reported dropped the
second's hop and the second committed the refused host's choice untainted. Each session's context now
keeps its own list, and a hop is dropped only when its page is the one being reported (which claims it)
or is attached already. Sessions are made by create_session on fake contexts, so the handlers it puts on
each context are the ones that run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.test_allowed_hosts_provenance import UnframedRequest
from tests.test_allowed_hosts_rechecks import ALLOWED, REFUSED, EventPage, manager_for
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage

# Where a refused host's redirect sent the popup: it can carry what the refused host chose.
CHOSEN = f"{ALLOWED}?d=REFUSED-CONTENT"


class Wired:
    """A session create_session made on a fake context, which delivers events to the handlers it was given."""

    def __init__(self, session, context: FakeContext) -> None:
        self.session = session
        self.context = context

    def fire(self, event: str, arg) -> None:
        for call in self.context.on.call_args_list:
            if call.args[0] == event:
                call.args[1](arg)

    def report(self, manager, popup: EventPage) -> None:
        """The context reports the popup, and its allowed opener judges it, as Playwright does."""
        self.fire("page", popup)
        manager.page_provenance.opener_judged(popup, opener_refused=False)


@pytest.fixture
def manager(tmp_path: Path):
    manager = manager_for(tmp_path)
    manager.audit.append = AsyncMock()
    manager._persist_session = AsyncMock()
    manager._settle = AsyncMock()
    manager._maybe_provision_session_tunnel = AsyncMock()
    return manager


async def wired_session(manager) -> Wired:
    context = FakeContext(FakePage())
    manager._acquire_session_browser = AsyncMock(return_value=(FakeBrowser(context), None))
    summary = await manager.create_session(name="s", start_url=f"{ALLOWED}/")
    return Wired(manager.sessions[summary["id"]], context)


async def test_a_hop_of_a_popup_reported_second_is_not_dropped_by_the_popup_reported_first(manager) -> None:
    wired = await wired_session(manager)
    first, second = EventPage(ALLOWED), EventPage(CHOSEN)
    hop = UnframedRequest(REFUSED)
    wired.fire("request", hop)
    hop.reported(second)  # the driver names the hop's frame before the context reports its page

    wired.report(manager, first)
    wired.report(manager, second)

    assert not manager._page_refused(first)
    assert manager._page_refused(second)


async def test_a_hop_one_sessions_context_saw_is_kept_by_that_session_alone(manager) -> None:
    one, two = await wired_session(manager), await wired_session(manager)
    popup_one, popup_two = EventPage(CHOSEN), EventPage(ALLOWED)
    hop = UnframedRequest(REFUSED)
    one.fire("request", hop)
    hop.reported(popup_one)

    two.report(manager, popup_two)

    assert (one.session.unframed_hops, two.session.unframed_hops) == ([hop], [])
    one.report(manager, popup_one)
    assert manager._page_refused(popup_one) and not manager._page_refused(popup_two)
    assert one.session.unframed_hops == []


async def test_a_kept_hop_whose_page_is_attached_already_is_dropped(manager) -> None:
    """Its page was attached before the hop was claimed, so the page's own listeners saw the request."""
    wired = await wired_session(manager)
    hop, pending = UnframedRequest(REFUSED), UnframedRequest(f"{REFUSED}?2")
    wired.fire("request", hop)
    wired.fire("request", pending)
    hop.reported(wired.session.page)

    wired.report(manager, EventPage(ALLOWED))

    assert wired.session.unframed_hops == [pending]
