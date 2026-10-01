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
                )
                session = SimpleNamespace(
                    page=page, totp_secret="JBSWY3DPEHPK3PXP", totp_hosts=(HOST,), mouse_position=(5.0, 5.0)
                )
                result = await BrowserActionService(manager).maybe_handle_totp(session)
                await asyncio.sleep(0.3)
                return {"result": result, "title": await page.title(), "promo": await page.input_value("[name=promo_code]")}
            finally:
                await browser.close()

    return asyncio.run(main())


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
