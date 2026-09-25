"""Elements are named the way a person reads the page, and never by their value.

The observation scripts named a field from aria-label, then placeholder, then
its text, then **its value**. So a login form's fields were called
``you@example.com``, ``pw`` and ``on`` instead of Email, Password and Remember
me, and a password typed into a field without a placeholder became that field's
name in every later observation (interactables, active element and form
outline) — reaching model prompts, MCP clients and logs although the type action
had redacted it.

The naming helper is exercised two ways: under Node against minimal fake DOM
elements (always, where Node exists — CI has it), and in a real Chromium when a
browser binary is available.
"""

from __future__ import annotations

import asyncio
import glob
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import browser_scripts
from app.browser.dom_pruner import _TYPE_PRIORITY

NODE = shutil.which("node")

# Minimal stand-ins for DOM elements: just the members abName/abRole/abChecked read.
_FAKE_DOM = r"""
const byId = {};
const document = { getElementById: (id) => byId[id] || null };
function el(tag, { attrs = {}, text = '', value = '', labels = [], checked = false, children = [], editable = false } = {}) {
  return {
    tagName: tag.toUpperCase(),
    innerText: text,
    textContent: text,
    value,
    checked,
    multiple: false,
    size: 0,
    isContentEditable: editable,
    labels,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    hasAttribute: (name) => name in attrs,
    querySelector: (sel) => (sel === 'img[alt]' ? children.find((c) => c.getAttribute('alt') !== null) || null : null),
  };
}
function label(text) {
  return { cloneNode: () => ({ textContent: text, querySelectorAll: () => [] }) };
}
byId.lbl = el('span', { text: 'Search terms' });
const cases = {
  labelled_email: el('input', { attrs: { type: 'email', placeholder: 'you@example.com' }, labels: [label('Email')] }),
  typed_password_no_placeholder: el('input', { attrs: { type: 'password', name: 'secret' }, value: 'hunter2' }),
  typed_text_no_hints: el('input', { attrs: { type: 'text' }, value: 'private note' }),
  checkbox_in_label: el('input', { attrs: { type: 'checkbox' }, value: 'on', labels: [label('Remember me')], checked: true }),
  labelled_by: el('input', { attrs: { 'aria-labelledby': 'lbl' } }),
  submit_caption: el('input', { attrs: { type: 'submit' }, value: 'Sign in' }),
  icon_button: el('button', { children: [el('img', { attrs: { alt: 'Settings' } })] }),
  link: el('a', { attrs: { href: '/help' }, text: 'Need help?' }),
  switch_widget: el('div', { attrs: { role: 'switch', 'aria-checked': 'false' }, text: 'Dark mode' }),
  labelled_editor: el('div', { attrs: { contenteditable: 'true', 'aria-label': 'Message body' }, text: 'draft one', editable: true }),
  unlabelled_editor: el('div', { attrs: { contenteditable: 'true' }, text: 'draft two', editable: true }),
  aria_textbox: el('div', { attrs: { role: 'textbox' }, text: 'typed words' }),
  custom_combobox: el('div', { attrs: { role: 'combobox', 'aria-label': 'Country' }, text: 'United Kingdom' }),
};
const out = {};
for (const [key, node] of Object.entries(cases)) {
  out[key] = { name: abName(node), role: abRole(node), checked: abChecked(node) };
}
console.log(JSON.stringify(out));
"""


def _run_naming_under_node() -> dict:
    source = browser_scripts._ELEMENT_NAMING_JS + _FAKE_DOM
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(source)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, temp path
            [NODE, handle.name], capture_output=True, text=True, timeout=30
        )
    finally:
        Path(handle.name).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_names_come_from_labels_captions_and_text() -> None:
    named = _run_naming_under_node()

    assert named["labelled_email"]["name"] == "Email"
    assert named["checkbox_in_label"] == {"name": "Remember me", "role": "checkbox", "checked": True}
    assert named["labelled_by"]["name"] == "Search terms"
    assert named["submit_caption"] == {"name": "Sign in", "role": "button", "checked": None}
    assert named["icon_button"]["name"] == "Settings"
    assert named["link"] == {"name": "Need help?", "role": "link", "checked": None}
    assert named["switch_widget"] == {"name": "Dark mode", "role": "switch", "checked": False}


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_a_typed_value_is_never_a_name() -> None:
    named = _run_naming_under_node()

    assert named["typed_password_no_placeholder"]["name"] == "secret"
    assert named["typed_text_no_hints"]["name"] == ""
    assert "hunter2" not in json.dumps(named)
    assert "private note" not in json.dumps(named)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_editors_and_textbox_widgets_are_not_named_by_their_content() -> None:
    # A contenteditable editor's text, or an ARIA textbox's, is what was typed.
    named = _run_naming_under_node()

    assert named["labelled_editor"]["name"] == "Message body"
    assert named["unlabelled_editor"]["name"] == ""
    assert named["aria_textbox"] == {"name": "", "role": "textbox", "checked": None}
    assert named["custom_combobox"]["name"] == "Country"
    for typed in ("draft one", "draft two", "typed words", "United Kingdom"):
        assert typed not in json.dumps(named), typed


def test_the_three_observation_scripts_share_the_helper() -> None:
    for name in ("INTERACTABLES_SCRIPT", "ACTIVE_ELEMENT_SCRIPT", "PAGE_SUMMARY_SCRIPT"):
        source = getattr(browser_scripts, name)
        assert "function abName(el)" in source, name
        assert "__ELEMENT_NAMING__" not in source, name
        # The old fallback chains read `el.value` / `field.value` directly.
        assert "|| el.value" not in source and "|| field.value" not in source, name


def test_the_pruner_ranks_the_reported_aria_roles() -> None:
    for role in ("textbox", "searchbox", "link", "button", "checkbox", "combobox", "switch", "tab"):
        assert role in _TYPE_PRIORITY, role


def _chromium() -> str | None:
    candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return candidates[-1] if candidates else None


@pytest.mark.skipif(_chromium() is None, reason="no local Chromium binary")
def test_real_chromium_names_a_login_form_and_leaks_nothing_typed() -> None:
    from playwright.async_api import async_playwright

    html = """<form>
      <label for=email>Email</label><input id=email type=email placeholder=you@example.com>
      <label for=pw>Password</label><input id=pw type=password>
      <input id=bare type=password name=secret>
      <label><input type=checkbox id=rem> Remember me</label>
      <label>Country <select><option>US</option><option>UK</option></select></label>
      <button type=submit>Sign in</button></form>
      <div id=editor contenteditable=true></div>"""

    async def run() -> tuple[list, dict, dict]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=_chromium())
            try:
                page = await browser.new_page()
                await page.set_content(html)
                await page.fill("#pw", "hunter2")
                await page.fill("#bare", "s3cr3t-typed")
                await page.fill("#editor", "draft-typed")
                editor_items = await page.evaluate(browser_scripts.INTERACTABLES_SCRIPT, 40)
                editor_active = await page.evaluate(browser_scripts.ACTIVE_ELEMENT_SCRIPT)
                await page.focus("#bare")
                items = await page.evaluate(browser_scripts.INTERACTABLES_SCRIPT, 40)
                active = await page.evaluate(browser_scripts.ACTIVE_ELEMENT_SCRIPT)
                summary = await page.evaluate(browser_scripts.PAGE_SUMMARY_SCRIPT, 500)
                return items, active, summary, editor_items, editor_active
            finally:
                await browser.close()

    items, active, summary, editor_items, editor_active = asyncio.run(run())

    labels = [item["label"] for item in items]
    assert labels[:5] == ["Email", "Password", "secret", "Remember me", "Country"]
    # The summary carries the same focused element, so an observation needs no
    # separate evaluate for it.
    assert summary["active_element"] == active
    assert summary["title"] == ""
    blob = json.dumps([items, active, summary])
    assert "hunter2" not in blob and "s3cr3t-typed" not in blob
    # An unlabelled rich-text editor holding typed text falls back to its id,
    # never to that text (which is page content, so it does appear in the
    # page's text excerpt).
    editor = next(item for item in editor_items if item["role"] == "textbox" and item["tag"] == "div")
    assert editor["label"] == "editor"
    assert editor_active["label"] == "editor"
    assert "draft-typed" not in json.dumps([editor_items, editor_active, summary["dom_outline"]])
