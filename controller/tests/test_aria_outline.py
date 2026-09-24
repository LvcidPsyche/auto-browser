"""The accessibility outline is built from Playwright's ARIA snapshot again.

It came from ``page.accessibility.snapshot()``, which the pinned Playwright no
longer has, so every outline read ``available: false``: the documented
accessibility tree was missing, the OCR skip that depends on it never applied,
and the focus-change verification signal could not fire. The snapshot that
replaces it carries field values — a filled password field reads
``textbox "Secret": hunter2`` — so the outline must keep roles, names and
states only.

The fixtures below are real ``page.aria_snapshot(mode="ai")`` output from
Chromium 1194 / Playwright 1.62.
"""

from __future__ import annotations

import asyncio
import glob
import json

import pytest
from playwright.async_api import Page

from app.browser.aria_outline import outline_from_aria_snapshot
from app.browser.services.observation import BrowserObservationService

FORM_SNAPSHOT = r"""- generic [ref=e1]:
  - navigation [ref=e2]:
    - list [ref=e3]:
      - listitem [ref=e4]:
        - link "Alpha" [ref=e5] [cursor=pointer]:
          - /url: /a
      - listitem [ref=e6]:
        - link "Beta \"quoted\"" [ref=e7] [cursor=pointer]:
          - /url: /b
  - main [ref=e8]:
    - 'heading "Section: one" [level=2] [ref=e9]'
    - paragraph [ref=e10]: Plain paragraph text.
    - generic [ref=e11]:
      - text: Secret
      - textbox "Secret" [ref=e12]: hunter2
    - generic [ref=e13]:
      - text: Notes
      - textbox "Notes" [ref=e14]: private note line2
    - generic [ref=e15]: editable body
    - group [ref=e16]:
      - generic "More" [ref=e17]
      - text: Hidden-ish
    - button "Off" [disabled] [ref=e18]
    - button "Bold" [pressed] [ref=e19]
    - searchbox "Find" [active] [ref=e20]: query text
    - img "Logo" [ref=e21]
"""

TABLE_SNAPSHOT = r"""- generic [active] [ref=f1e1]:
  - navigation [ref=f1e2]:
    - link "Log out" [ref=f1e3] [cursor=pointer]:
      - /url: /login.html
    - text: "|"
    - link "Export CSV" [ref=f1e4] [cursor=pointer]:
      - /url: /report.csv
  - heading "Orders" [level=1] [ref=f1e5]
  - table [ref=f1e6]:
    - rowgroup [ref=f1e7]:
      - row [ref=f1e8]:
        - columnheader "ID" [ref=f1e9]
        - columnheader "Total" [ref=f1e11]
    - rowgroup [ref=f1e13]:
      - row [ref=f1e14]:
        - cell "1001" [ref=f1e15]
        - cell "$120.00" [ref=f1e17]
      - row [ref=f1e19]:
        - cell "1002" [ref=f1e20]
        - cell "$89.50" [ref=f1e22]
  - combobox [ref=f1e29]:
    - option "All" [selected]
    - option "Shipped"
  - checkbox "Notify" [checked=mixed] [ref=f1e31]
  - button "Request refund" [ref=f1e30]
  - paragraph
"""

TYPED_VALUES = ("hunter2", "private note", "query text")


def _by_name(outline: dict) -> dict[str, dict]:
    return {node["name"]: node for node in outline["nodes"] if "name" in node}


def test_field_values_never_reach_the_outline() -> None:
    outline = outline_from_aria_snapshot(FORM_SNAPSHOT)

    dumped = json.dumps(outline)
    for value in TYPED_VALUES:
        assert value not in dumped, value
    # Text runs are the excerpt's job, and properties are not elements.
    assert "Plain paragraph text" not in dumped
    assert "/url" not in dumped and "ref=" not in dumped


def test_roles_names_and_states_are_parsed() -> None:
    outline = outline_from_aria_snapshot(FORM_SNAPSHOT)
    named = _by_name(outline)

    assert outline["available"] is True
    assert named["Section: one"] == {"role": "heading", "name": "Section: one", "level": 2, "depth": 1}
    assert named['Beta "quoted"']["role"] == "link"
    assert named["Secret"]["role"] == "textbox"
    assert named["Off"]["disabled"] is True
    assert named["Bold"]["pressed"] is True
    assert named["Logo"]["role"] == "img"


def test_the_active_element_is_the_focused_node() -> None:
    outline = outline_from_aria_snapshot(FORM_SNAPSHOT)

    assert outline["focused"]["name"] == "Find"
    assert outline["focused"]["role"] == "searchbox"


def test_depth_counts_kept_ancestors_only() -> None:
    named = _by_name(outline_from_aria_snapshot(FORM_SNAPSHOT))

    # navigation > list > (listitem, dropped) > link; the outer generic is dropped too.
    assert named["Alpha"]["depth"] == 2
    # main > (generic, dropped) > textbox
    assert named["Secret"]["depth"] == 1


def test_layout_wrappers_and_cells_are_counted_but_not_listed() -> None:
    outline = outline_from_aria_snapshot(TABLE_SNAPSHOT)
    roles = [node["role"] for node in outline["nodes"]]

    assert "cell" not in roles and "row" not in roles and "generic" not in roles
    assert outline["role_counts"]["cell"] == 4
    assert outline["role_counts"]["row"] == 3
    assert "generic" not in outline["role_counts"]
    assert {"table", "combobox", "columnheader"} <= set(roles)
    assert _by_name(outline)["All"]["selected"] is True
    assert _by_name(outline)["Notify"]["checked"] == "mixed"
    # A focused wrapper (body) is not a focused control.
    assert outline["focused"] is None


def test_the_node_list_is_capped_but_counts_are_not() -> None:
    snapshot = "".join(f'- link "Item {index}" [ref=e{index}]\n' for index in range(50))

    outline = outline_from_aria_snapshot(snapshot, limit=30)

    assert len(outline["nodes"]) == 30
    assert outline["role_counts"]["link"] == 50


def test_unknown_lines_are_ignored() -> None:
    outline = outline_from_aria_snapshot('not yaml\n- ???\n- button "Go"\n')

    assert [node["name"] for node in outline["nodes"]] == ["Go"]


def test_the_pinned_playwright_provides_the_snapshot_api() -> None:
    # page.accessibility disappeared in a routine bump without a test noticing.
    assert callable(getattr(Page, "aria_snapshot", None))


def test_a_page_without_the_api_reports_unavailable() -> None:
    class OldPage:
        pass

    service = BrowserObservationService(manager=None)
    outline = asyncio.run(service.accessibility_outline(OldPage()))  # type: ignore[arg-type]

    assert outline["available"] is False
    assert outline["error"] == "accessibility_snapshot_unavailable"


def _chromium() -> str | None:
    candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return candidates[-1] if candidates else None


@pytest.mark.skipif(_chromium() is None, reason="no local Chromium binary")
def test_real_chromium_outline_of_a_filled_form() -> None:
    from playwright.async_api import async_playwright

    html = """
      <h1>Sign in</h1>
      <label>Email <input id="email" type="email"></label>
      <label>Password <input id="pw" type="password"></label>
      <label><input id="remember" type="checkbox"> Remember me</label>
      <button>Sign in</button>
    """

    async def run() -> dict:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=_chromium())
            try:
                page = await browser.new_page()
                await page.set_content(html)
                await page.fill("#email", "alice@example.com")
                await page.fill("#pw", "hunter2")
                await page.check("#remember")
                return await BrowserObservationService(manager=None).accessibility_outline(page)
            finally:
                await browser.close()

    outline = asyncio.run(run())

    assert outline["available"] is True
    assert "hunter2" not in json.dumps(outline)
    assert "alice@example.com" not in json.dumps(outline)
    named = _by_name(outline)
    assert named["Password"]["role"] == "textbox"
    assert named["Remember me"]["checked"] is True
    assert outline["focused"]["name"] == "Remember me"
