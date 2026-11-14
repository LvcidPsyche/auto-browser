"""The TOTP autofill types a code only into a one-time-code field, and submits only its form.

It runs after every action on a TOTP host. It used to type a live code into
the first visible field whose name, id, label or placeholder contained "code" —
a promo code, a ZIP code, a card security code — and then click the first
`button[type=submit]` on the page. On a checkout page with no one-time-code
field at all, a scroll typed the code into "Promo code" and clicked
"Place order", with no approval anywhere in that path.
"""

from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace

import pytest

from app.browser.services.actions import BrowserActionService, is_one_time_code_field
from tests._chromium import chromium_executable, requires_chromium

HOST = "shop.example.com"


@pytest.mark.parametrize(
    "attributes",
    [
        {"autocomplete": "one-time-code"},
        {"name": "otp"},
        {"id": "totp-input"},
        {"name": "code", "inputmode": "numeric"},
        {"id": "verification_code"},
        {"placeholder": "6-digit code"},
        {"aria-label": "Authentication code", "type": "tel"},
    ],
)
def test_one_time_code_fields_are_recognised(attributes: dict[str, str]) -> None:
    assert is_one_time_code_field(attributes)


@pytest.mark.parametrize(
    "attributes",
    [
        {"name": "promo_code"},
        {"name": "zipcode"},
        {"id": "postal-code"},
        {"name": "coupon_code"},
        {"name": "gift_card_code"},
        {"name": "security_code", "autocomplete": "cc-csc"},
        {"name": "cvv_code"},
        {"id": "country_code"},
        {"name": "area_code"},
        {"placeholder": "Referral code"},
        {"name": "code", "type": "email"},
        {"name": "otp", "type": "hidden"},
    ],
)
def test_other_code_fields_are_not(attributes: dict[str, str]) -> None:
    assert not is_one_time_code_field(attributes)


CHECKOUT = """<!doctype html>
<form onsubmit="event.preventDefault(); document.title='ORDER PLACED'">
  <label>Promo code <input name="promo_code" placeholder="Promo code"></label>
  <label>ZIP <input name="zipcode"></label>
  <button type="submit">Place order ($499)</button>
</form>"""

CHECKOUT_THEN_OTP = """<!doctype html>
<form onsubmit="event.preventDefault(); document.title='ORDER PLACED'">
  <input name="promo_code" placeholder="Promo code">
  <button type="submit">Place order ($499)</button>
</form>
<form onsubmit="event.preventDefault(); document.title='VERIFIED:' + this.otp.value">
  <input name="otp" autocomplete="one-time-code" inputmode="numeric" maxlength="6">
  <button type="submit">Verify</button>
</form>"""


def _autofill(html: str) -> dict:
    from playwright.async_api import async_playwright

    async def main() -> dict:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                page = await browser.new_page()
                page.set_default_timeout(1500)
                await page.route(f"https://{HOST}/**", lambda route: route.fulfill(body=html, content_type="text/html"))
                await page.goto(f"https://{HOST}/checkout")

                async def settle(_page) -> None:
                    await asyncio.sleep(0.2)

                manager = SimpleNamespace(
                    settings=SimpleNamespace(
                        human_typing_min_delay_ms=1,
                        human_typing_max_delay_ms=2,
                        default_viewport_width=1280,
                        default_viewport_height=720,
                    ),
                    _settle=settle,
                    _page_refused=lambda _page: False,
                    _shown_url=lambda page: page.url,
                )
                session = SimpleNamespace(
                    page=page, totp_secret="JBSWY3DPEHPK3PXP", totp_hosts=(HOST,), mouse_position=(5.0, 5.0)
                )
                result = await BrowserActionService(manager).maybe_handle_totp(session)
                await asyncio.sleep(0.3)
                return {
                    "result": result,
                    "title": await page.title(),
                    "promo": await page.input_value("[name=promo_code]"),
                }
            finally:
                await browser.close()

    return asyncio.run(main())


OTHER_HOST = "collect.example.net"
# A named host's one-time-code field that sends the page to another host when it is pressed, and that host's field,
# which the autofill's selector matches too.
LEAVES_ON_PRESS = f"""<!doctype html>
<form onsubmit="event.preventDefault(); document.title='SUBMITTED ON THE NAMED HOST:' + this.otp.value">
  <input name="otp" autocomplete="one-time-code" inputmode="numeric" maxlength="6"
         onmousedown="location.href='https://{OTHER_HOST}/collect'">
  <button type="submit">Verify</button>
</form>"""
COLLECTS = """<!doctype html>
<form onsubmit="event.preventDefault(); document.title='RECEIVED:' + this.otp.value">
  <input name="otp" autocomplete="one-time-code" autofocus>
</form>"""


async def _fulfil(body: str, route) -> None:
    await route.fulfill(body=body, content_type="text/html")


def _autofill_where_the_press_leaves(runs: int) -> list[dict]:
    """The autofill on LEAVES_ON_PRESS, ``runs`` times in one browser, each on a page of its own."""
    from playwright.async_api import async_playwright

    async def main() -> list[dict]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                return [await one(browser) for _ in range(runs)]
            finally:
                await browser.close()

    async def one(browser) -> dict:
        page = await browser.new_page()
        page.set_default_timeout(3000)
        for host, body in ((HOST, LEAVES_ON_PRESS), (OTHER_HOST, COLLECTS)):
            await page.route(f"https://{host}/**", partial(_fulfil, body))
        await page.goto(f"https://{HOST}/2fa")

        async def settle(_page) -> None:
            await asyncio.sleep(0.3)

        manager = SimpleNamespace(
            settings=SimpleNamespace(
                human_typing_min_delay_ms=40,
                human_typing_max_delay_ms=80,
                default_viewport_width=1280,
                default_viewport_height=720,
            ),
            _settle=settle,
            _page_refused=lambda _page: False,
            _shown_url=lambda page: page.url,
        )
        session = SimpleNamespace(
            page=page, totp_secret="JBSWY3DPEHPK3PXP", totp_hosts=(HOST,), mouse_position=(5.0, 5.0)
        )
        result = await BrowserActionService(manager).maybe_handle_totp(session)
        await asyncio.sleep(0.5)
        field = await page.input_value("input[name=otp]") if OTHER_HOST in page.url else None
        seen = {"result": result, "url": page.url, "title": await page.title(), "other_host_field": field}
        await page.close()
        return seen

    return asyncio.run(main())


@requires_chromium
def test_a_page_that_leaves_the_named_host_as_its_field_is_pressed_gets_no_code() -> None:
    # The host was checked before the field was looked for and after, and not after the click that focuses it: the
    # press sent the page to another host, whose field the code was typed into and submitted, every time once the
    # browser was warm (10 of 10). A fresh browser's first click can run out of its time before the press and focus
    # the field without one, so the page never moves (and the code goes into the named host's field, as it should):
    # the runs share one browser, and every one must hold. Three runs that all focused so would pass with nothing
    # tested: one at least must have reached the other host's page, its field there and empty.
    outcomes = _autofill_where_the_press_leaves(3)
    assert any(OTHER_HOST in seen["url"] and seen["other_host_field"] == "" for seen in outcomes), outcomes
    for seen in outcomes:
        assert not seen["title"].startswith("RECEIVED:") and not seen["other_host_field"], seen


@requires_chromium
def test_a_checkout_page_gets_no_code_and_no_click() -> None:
    outcome = _autofill(CHECKOUT)
    assert outcome["result"] is None
    assert outcome["title"] != "ORDER PLACED"
    assert outcome["promo"] == ""


@requires_chromium
def test_the_code_goes_into_the_one_time_code_field_and_only_its_form_submits() -> None:
    outcome = _autofill(CHECKOUT_THEN_OTP)
    assert outcome["result"] is not None
    assert outcome["promo"] == ""
    assert outcome["title"].startswith("VERIFIED:")
    assert len(outcome["title"].removeprefix("VERIFIED:")) == 6
