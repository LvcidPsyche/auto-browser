"""Clicks and keystrokes land on the element the action names, or nowhere.

The human-like path resolved an element's centre and then pressed the mouse at
that point, which sends the click to whatever is on top there, and typed into
whatever then had focus. A transparent element over a "Save draft" button took
the click, and text meant for one field went into another — while the approval
and the audit trail named the element the caller chose.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.action_errors import BrowserActionError
from app.browser.services.actions import BrowserActionService
from tests._chromium import chromium_executable, requires_chromium

PAGE = """<!doctype html><body style="margin:40px">
<input id="target" style="width:300px;height:30px">
<input id="trap" style="position:absolute;left:40px;top:40px;width:300px;height:30px;opacity:0;z-index:9">
<input id="plain" style="display:block;margin-top:20px;width:300px;height:30px">
<input id="thief" style="display:block;margin-top:20px;width:300px;height:30px"
  onfocus="document.querySelector('#stolen').focus()">
<input id="stolen" style="display:block;margin-top:20px;width:300px;height:30px">
<button id="safe" style="display:block;margin-top:20px;width:200px;height:40px" onclick="document.title='SAFE'">Save</button>
<button id="evil" style="position:absolute;width:200px;height:40px;opacity:0;z-index:9" onclick="document.title='EVIL'">x</button>
<button id="open" style="display:block;margin-top:20px;width:200px;height:40px" onclick="document.title='OPEN'">Open</button>
<script>
  const r = document.querySelector('#safe').getBoundingClientRect();
  const evil = document.querySelector('#evil');
  evil.style.left = r.left + 'px'; evil.style.top = (r.top + window.scrollY) + 'px';
</script></body>"""


def _service(page) -> tuple[BrowserActionService, SimpleNamespace]:
    session = SimpleNamespace(id="s", page=page, mouse_position=(5.0, 5.0))

    async def run_action(_session, _name, target, operation):
        await operation()
        return target

    async def settle(_page) -> None:
        return None

    manager = SimpleNamespace(
        settings=SimpleNamespace(
            human_typing_min_delay_ms=1,
            human_typing_max_delay_ms=2,
            default_viewport_width=1280,
            default_viewport_height=720,
        ),
        get_session=lambda _sid: _as_awaitable(session),
        _run_action=run_action,
        _settle=settle,
    )
    return BrowserActionService(manager), session


async def _as_awaitable(value):
    return value


def _run(scenario) -> dict:
    from playwright.async_api import async_playwright

    async def main() -> dict:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                page = await browser.new_page()
                page.set_default_timeout(1500)
                await page.set_content(PAGE)
                service, _session = _service(page)
                return await scenario(service, page)
            finally:
                await browser.close()

    return asyncio.run(main())


@requires_chromium
def test_a_covered_button_is_not_clicked_through_its_cover() -> None:
    async def scenario(service, page) -> dict:
        error = None
        try:
            await service.click("s", selector="#safe")
        except Exception as exc:  # noqa: BLE001 - which error does not matter, the click must not happen
            error = exc
        return {"title": await page.title(), "error": error}

    result = _run(scenario)
    assert result["title"] != "EVIL"
    assert result["error"] is not None


@requires_chromium
def test_an_uncovered_button_is_clicked() -> None:
    async def scenario(service, page) -> dict:
        await service.click("s", selector="#open")
        return {"title": await page.title()}

    assert _run(scenario)["title"] == "OPEN"


@requires_chromium
def test_text_for_a_covered_field_goes_into_that_field_or_nowhere() -> None:
    async def scenario(service, page) -> dict:
        try:
            await service.type("s", selector="#target", text="hunter2")
        except BrowserActionError:
            pass
        return {"target": await page.input_value("#target"), "trap": await page.input_value("#trap")}

    result = _run(scenario)
    assert result["trap"] == ""
    assert result["target"] in {"", "hunter2"}


@requires_chromium
def test_typing_into_a_plain_field_still_works() -> None:
    async def scenario(service, page) -> dict:
        await service.type("s", selector="#plain", text="hello")
        return {"plain": await page.input_value("#plain")}

    assert _run(scenario)["plain"] == "hello"


@requires_chromium
def test_nothing_is_typed_when_the_field_will_not_keep_focus() -> None:
    async def scenario(service, page) -> dict:
        with pytest.raises(BrowserActionError):
            await service.type("s", selector="#thief", text="hunter2")
        return {"thief": await page.input_value("#thief"), "stolen": await page.input_value("#stolen")}

    result = _run(scenario)
    assert result == {"thief": "", "stolen": ""}
