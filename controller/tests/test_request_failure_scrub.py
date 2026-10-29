"""A failed request's URL is scrubbed before it is kept, as the network inspector scrubs a request URL.

`session.request_failures` keeps the URL of every request that failed, and serves it (get_request_failures)
and puts the last ten into every observation. It kept `request.url` as the page asked for it, so a query
holding `access_token=...` sat there in the clear whatever NETWORK_INSPECTOR_* and PII_SCRUB_NETWORK said.
The URL is now stored through the inspector's own URL scrub (query and fragment, the same size cap), and under
the same setting: with PII_SCRUB_NETWORK off, the inspector's URLs and these are both stored as they are.
"""

from __future__ import annotations

import asyncio
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browser.page_provenance import PageProvenance
from app.browser.services.diagnostics import BrowserDiagnosticsService
from app.browser.services.observation import BrowserObservationService
from app.browser_scripts import INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT
from app.pii_scrub import PiiScrubber
from tests._fake_element_ids import FakePageElementIds

BODY_MAX_BYTES = 16384
SECRET_URL = "https://app.test/api/items?access_token=Opaque-AT-123456&page=2"
SCRUBBED_URL = "https://app.test/api/items?access_token=%5BREDACTED%5D&page=2"


class FakePage:
    def __init__(self) -> None:
        self.url = "https://app.test/"
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event: str, argument: object) -> None:
        for handler in self.handlers.get(event, []):
            handler(argument)

    async def title(self) -> str:
        return "App"

    async def aria_snapshot(self, **_: object) -> str:
        return '- heading "App" [level=1] [ref=e1]\n'

    async def evaluate(self, script: str, *args: object) -> object:
        if script is INTERACTABLES_SCRIPT:
            return []
        if script is PAGE_SUMMARY_SCRIPT:
            return {"title": "App", "active_element": {"tag": "body"}, "text_excerpt": "", "dom_outline": {}}
        raise AssertionError(f"unexpected script passed to evaluate: {script!r}")


class FakeRequest:
    def __init__(self, url: str, method: str = "GET", failure: str | None = "net::ERR_FAILED") -> None:
        self.url = url
        self.method = method
        self.failure = failure
        self.resource_type = "fetch"

    def is_navigation_request(self) -> bool:
        return False


def _setup(
    scrubber: PiiScrubber | None, max_bytes: int = BODY_MAX_BYTES
) -> tuple[BrowserDiagnosticsService, SimpleNamespace, FakePage]:
    session = SimpleNamespace(
        id="session-1",
        lock=asyncio.Lock(),
        attached_pages=weakref.WeakSet(),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
    )
    page = FakePage()
    session.page = page
    manager = SimpleNamespace(
        settings=SimpleNamespace(
            network_inspector_body_max_bytes=max_bytes,
            perception_preset_default="text",
            ocr_skip_when_text_available=True,
        ),
        get_session=AsyncMock(return_value=session),
        _session_summary=AsyncMock(return_value={"id": "session-1"}),
        _current_takeover_url=MagicMock(return_value=None),
        _assert_page_allowed=MagicMock(),
        _url_off_list=MagicMock(return_value=False),
        _page_refused=MagicMock(return_value=False),
        page_provenance=PageProvenance(),
        remote_access=SimpleNamespace(session_info=MagicMock(return_value={})),
        tabs=SimpleNamespace(summaries=AsyncMock(return_value=[])),
        element_ids=FakePageElementIds(),
    )
    service = BrowserDiagnosticsService(manager, scrubber, None)  # type: ignore[arg-type]
    service.attach_page_listeners(page, session)  # type: ignore[arg-type]
    return service, session, page


def test_a_failed_request_is_stored_with_the_secrets_of_its_query_redacted() -> None:
    _, session, page = _setup(PiiScrubber())

    page.fire("requestfailed", FakeRequest(SECRET_URL, method="POST", failure="net::ERR_ABORTED"))

    assert session.request_failures == [{"url": SCRUBBED_URL, "method": "POST", "failure": "net::ERR_ABORTED"}]


def test_a_failed_request_with_no_reason_is_stored_with_none() -> None:
    _, session, page = _setup(PiiScrubber())

    page.fire("requestfailed", FakeRequest("https://app.test/ok?page=2", failure=None))

    assert session.request_failures == [{"url": "https://app.test/ok?page=2", "method": "GET", "failure": None}]


def test_the_fragment_of_a_failed_request_is_scrubbed_like_its_query() -> None:
    _, session, page = _setup(PiiScrubber())

    page.fire("requestfailed", FakeRequest("https://app.test/cb#access_token=Opaque-AT-123456&state=s"))

    assert session.request_failures[0]["url"] == "https://app.test/cb#access_token=%5BREDACTED%5D&state=s"


def test_a_query_over_the_size_cap_is_replaced_not_stored() -> None:
    _, session, page = _setup(PiiScrubber(), max_bytes=64)

    page.fire("requestfailed", FakeRequest("https://app.test/p?token=" + "a" * 200))

    assert session.request_failures[0]["url"] == "https://app.test/p?[QUERY TOO LONG]"


def test_a_scrub_that_fails_costs_the_query() -> None:
    scrubber = PiiScrubber()
    scrubber.url = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    _, session, page = _setup(scrubber)

    page.fire("requestfailed", FakeRequest(SECRET_URL))

    assert session.request_failures[0]["url"] == "https://app.test/api/items"


@pytest.mark.parametrize(
    "scrubber",
    [None, PiiScrubber(network_enabled=False), PiiScrubber(enabled=False)],
    ids=["no scrubber", "PII_SCRUB_NETWORK off", "PII_SCRUB_ENABLED off"],
)
def test_with_network_scrubbing_off_the_url_is_stored_as_it_is_as_in_the_inspector(
    scrubber: PiiScrubber | None,
) -> None:
    _, session, page = _setup(scrubber)

    page.fire("requestfailed", FakeRequest(SECRET_URL))

    assert session.request_failures[0]["url"] == SECRET_URL


def test_the_list_keeps_its_bound_with_scrubbed_urls() -> None:
    _, session, page = _setup(PiiScrubber())

    for index in range(60):
        page.fire("requestfailed", FakeRequest(f"https://app.test/{index}?access_token=Opaque-AT-123456"))

    assert len(session.request_failures) == 50
    assert session.request_failures[-1]["url"] == "https://app.test/59?access_token=%5BREDACTED%5D"
    assert not any("Opaque-AT" in item["url"] for item in session.request_failures)


async def test_get_request_failures_serves_the_scrubbed_url() -> None:
    service, _, page = _setup(PiiScrubber())
    page.fire("requestfailed", FakeRequest(SECRET_URL))

    served = await service.get_request_failures("session-1")

    assert [item["url"] for item in served["items"]] == [SCRUBBED_URL]


async def test_an_observation_carries_the_scrubbed_url() -> None:
    service, session, page = _setup(PiiScrubber())
    page.fire("requestfailed", FakeRequest(SECRET_URL))

    payload = await BrowserObservationService(manager=service.manager).observation_payload(session, preset="text")

    assert [item["url"] for item in payload["request_failures"]] == [SCRUBBED_URL]
    assert "Opaque-AT" not in repr(payload)
