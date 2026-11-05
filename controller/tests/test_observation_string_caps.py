"""Page-controlled strings in an observation are capped where the script reads them.

`href`, `type`, `role`, the title, a form's `action` and `method`, and a field's or the focused element's `name` and
`id` went into the observation as the page wrote them. A page could make a megabyte-long attribute, and it reached
model prompts, MCP clients and logs; only the element count was bounded. The same held for the find_elements
scripts, the feed and profile extractors and the CDP asset references. The scripts are run under Node against
minimal fake DOM elements (no browser), as in tests/test_element_naming.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import browser_scripts
from app.browser_scripts import AB_ATTR_MAX, AB_HREF_MAX, AB_TITLE_MAX, AB_TRUNCATED_MARK

NODE = shutil.which("node")
HUGE = 1_000_000

# `%(...)s` slots take script sources; `%(huge)d` the fixture size; `%(mode)s` is "huge" or "short".
_RUNNER = r"""
const HUGE = %(huge)d;
const MODE = %(mode)s;
const big = (c, short) => (MODE === 'huge' ? c.repeat(HUGE) : short);
const bigUrl = (c) => (MODE === 'huge' ? 'https://example.com/' + c.repeat(HUGE) : 'https://example.com/a?b=c');
function fake(tag, attrs, extra) {
  return Object.assign({
    tagName: tag.toUpperCase(), innerText: 'label', textContent: 'label', value: '', checked: false, multiple: false,
    size: 0, isContentEditable: false, labels: [], disabled: false, className: '',
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    hasAttribute: (name) => name in attrs,
    querySelector: () => null,
    closest: () => null,
    getBoundingClientRect: () => ({ x: 1, y: 2, width: 30, height: 10, top: 2 }),
  }, extra);
}
const link = fake('a', { name: big('n', 'go'), type: big('t', 'button'), role: big('r', 'menuitem') }, {
  href: bigUrl('h'),
  id: big('i', 'link-1'),
  value: big('v', 'val'),
  className: big('c', 'btn primary'),
  innerText: 'a post long enough to count as one',
  querySelectorAll: (sel) => (
    sel === 'a[href]' ? [{ href: bigUrl('l') }]
      : sel === 'img[src]' ? [{ src: bigUrl('s') }] : []),
});
const field = fake('input', { type: big('y', 'text'), name: big('f', 'email') }, { id: big('j', 'email-field') });
const form = {
  getAttribute: (name) => (name === 'action' ? big('a', '/login') : name === 'method' ? big('m', 'post') : null),
  querySelectorAll: () => [field],
};
globalThis.window = {
  getComputedStyle: () => ({ visibility: 'visible', display: 'block', backgroundImage: big('b', 'none') }),
  location: { href: bigUrl('u') },
  scrollY: 0,
};
globalThis.NodeFilter = { SHOW_TEXT: 4 };
let walked = false;
globalThis.document = {
  activeElement: field,
  body: { innerText: 'body' },
  title: big('T', 'Sign in'),
  forms: [form],
  querySelectorAll: (sel) => (sel.startsWith('a,') || sel.startsWith('article') || sel === 'sel' ? [link] : []),
  querySelector: (sel) => {
    if (sel === 'target') {
      return fake('img', {}, { src: bigUrl('s'), href: bigUrl('k'), currentSrc: bigUrl('z') });
    }
    return sel.includes('avatar') ? { src: bigUrl('q') } : null;
  },
  createTreeWalker: () => ({
    nextNode: () => (walked ? null : ((walked = true), { textContent: 'needle', parentElement: link })),
  }),
};
const interactables = (%(interactables)s)(10, null);
const active = (%(active)s)(null, null);
const summary = (%(summary)s)(100, null);
const posts = (%(posts)s)(5);
const profile = (%(profile)s)();
const foundByText = (%(find_text)s)(['needle', false, 5, 5]);
const foundBySelector = (%(find_selector)s)(['sel', 5]);
const assets = (%(assets)s);
console.log(JSON.stringify({ interactables, active, summary, posts, profile, foundByText, foundBySelector, assets }));
"""


def _run_scripts(mode: str) -> dict:
    source = _RUNNER % {
        "huge": HUGE,
        "mode": json.dumps(mode),
        "interactables": browser_scripts.INTERACTABLES_SCRIPT,
        "active": browser_scripts.ACTIVE_ELEMENT_SCRIPT,
        "summary": browser_scripts.PAGE_SUMMARY_SCRIPT,
        "posts": browser_scripts.EXTRACT_POSTS_SCRIPT,
        "profile": browser_scripts.EXTRACT_PROFILE_SCRIPT,
        "find_text": browser_scripts.FIND_ELEMENTS_TEXT_SCRIPT,
        "find_selector": browser_scripts.FIND_ELEMENTS_SELECTOR_SCRIPT,
        "assets": browser_scripts.ELEMENT_ASSETS_SCRIPT.replace("__SELECTOR__", json.dumps("target")),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(source)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, temp path
            [NODE, handle.name], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
    finally:
        Path(handle.name).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def _within(value: str, cap: int) -> None:
    assert isinstance(value, str)
    assert 0 < len(value) <= cap


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_href_name_and_id_are_capped_in_every_observation_script() -> None:
    out = _run_scripts("huge")

    link = out["interactables"][0]
    _within(link["href"], AB_HREF_MAX)
    assert link["href"].startswith("https://example.com/")
    for active in (out["active"], out["summary"]["active_element"]):
        _within(active["name"], AB_ATTR_MAX)
        _within(active["id"], AB_ATTR_MAX)
    form_field = out["summary"]["dom_outline"]["forms"][0]["fields"][0]
    _within(form_field["name"], AB_ATTR_MAX)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_type_role_title_action_and_method_are_capped_in_the_observation() -> None:
    out = _run_scripts("huge")

    link = out["interactables"][0]
    _within(link["type"], AB_ATTR_MAX)
    _within(link["role"], AB_ATTR_MAX)
    summary = out["summary"]
    _within(summary["title"], AB_TITLE_MAX)
    form = summary["dom_outline"]["forms"][0]
    _within(form["action"], AB_HREF_MAX)
    _within(form["method"], AB_ATTR_MAX)
    _within(form["fields"][0]["type"], AB_ATTR_MAX)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_find_elements_scripts_cap_their_strings() -> None:
    out = _run_scripts("huge")

    for found in (out["foundByText"][0], out["foundBySelector"][0]):
        _within(found["href"], AB_HREF_MAX)
        _within(found["id"], AB_ATTR_MAX)
        _within(found["value"], AB_ATTR_MAX)
        _within(found["class"], AB_ATTR_MAX)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_feed_profile_and_cdp_asset_scripts_cap_their_urls() -> None:
    out = _run_scripts("huge")

    post = out["posts"][0]
    for url in (*post["links"], *post["images"]):
        _within(url, AB_HREF_MAX)
    _within(out["profile"]["url"], AB_HREF_MAX)
    _within(out["profile"]["avatar_url"], AB_HREF_MAX)
    _within(out["profile"]["page_title"], AB_TITLE_MAX)
    for key in ("src", "href", "currentSrc", "backgroundImage"):
        _within(out["assets"][key], AB_HREF_MAX)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_a_cut_url_is_marked_and_an_uncut_one_is_not() -> None:
    huge = _run_scripts("huge")

    assert huge["interactables"][0]["href"].endswith(AB_TRUNCATED_MARK)
    assert huge["summary"]["dom_outline"]["forms"][0]["action"].endswith(AB_TRUNCATED_MARK)
    assert huge["foundBySelector"][0]["href"].endswith(AB_TRUNCATED_MARK)
    assert huge["assets"]["src"].endswith(AB_TRUNCATED_MARK)
    assert not _run_scripts("short")["interactables"][0]["href"].endswith(AB_TRUNCATED_MARK)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_ordinary_values_are_unchanged() -> None:
    out = _run_scripts("short")

    link = out["interactables"][0]
    assert link["tag"] == "a"
    assert link["href"] == "https://example.com/a?b=c"
    assert link["type"] == "button"
    assert link["role"] == "menuitem"
    assert out["active"]["tag"] == "input"
    assert out["active"]["name"] == "email"
    assert out["active"]["id"] == "email-field"
    summary = out["summary"]
    assert summary["title"] == "Sign in"
    form = summary["dom_outline"]["forms"][0]
    assert form["action"] == "/login"
    assert form["method"] == "post"
    assert form["fields"][0]["type"] == "text"
    assert form["fields"][0]["name"] == "email"
    for found in (out["foundByText"][0], out["foundBySelector"][0]):
        assert (found["href"], found["id"], found["value"], found["class"]) == (
            "https://example.com/a?b=c",
            "link-1",
            "val",
            "btn primary",
        )
    assert out["posts"][0]["links"] == ["https://example.com/a?b=c"]
    assert out["profile"]["url"] == "https://example.com/a?b=c"
    assert out["assets"]["src"] == "https://example.com/a?b=c"
