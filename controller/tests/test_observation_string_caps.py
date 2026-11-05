"""Page-controlled strings in an observation are capped where the script reads them.

`href`, and a field's or the focused element's `name` and `id`, went into the observation as the page wrote them.
A page could make a megabyte-long attribute, and it reached model prompts, MCP clients and logs; only the element
count was bounded. The scripts are run under Node against minimal fake DOM elements (no browser), as in
tests/test_element_naming.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import browser_scripts

NODE = shutil.which("node")
HUGE = 1_000_000

_RUNNER = r"""
const HUGE = %(huge)d;
function fake(tag, attrs, extra) {
  return Object.assign({
    tagName: tag.toUpperCase(), innerText: 'label', textContent: 'label', value: '', checked: false, multiple: false,
    size: 0, isContentEditable: false, labels: [], disabled: false,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    hasAttribute: (name) => name in attrs,
    querySelector: () => null,
    closest: () => null,
    getBoundingClientRect: () => ({ x: 1, y: 2, width: 30, height: 10 }),
  }, extra);
}
const link = fake('a', { name: 'n'.repeat(HUGE) }, { href: 'https://example.com/' + 'h'.repeat(HUGE), id: 'i'.repeat(HUGE) });
const field = fake('input', { type: 'text', name: 'f'.repeat(HUGE) }, { id: 'j'.repeat(HUGE) });
globalThis.window = { getComputedStyle: () => ({ visibility: 'visible', display: 'block' }) };
globalThis.document = {
  activeElement: field,
  body: { innerText: 'body' },
  title: 't',
  forms: [{ getAttribute: () => null, querySelectorAll: () => [field] }],
  querySelectorAll: (sel) => (sel.startsWith('a,') ? [link] : []),
};
const interactables = (%(interactables)s)(10, null);
const active = (%(active)s)(null, null);
const summary = (%(summary)s)(100, null);
console.log(JSON.stringify({ interactables, active, summary }));
"""


def _run_scripts() -> dict:
    source = _RUNNER % {
        "huge": HUGE,
        "interactables": browser_scripts.INTERACTABLES_SCRIPT,
        "active": browser_scripts.ACTIVE_ELEMENT_SCRIPT,
        "summary": browser_scripts.PAGE_SUMMARY_SCRIPT,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(source)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, temp path
            [NODE, handle.name], capture_output=True, text=True, timeout=60
        )
    finally:
        Path(handle.name).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_href_name_and_id_are_capped_in_every_observation_script() -> None:
    out = _run_scripts()

    link = out["interactables"][0]
    assert 0 < len(link["href"]) <= 2000
    assert link["href"].startswith("https://example.com/")
    for active in (out["active"], out["summary"]["active_element"]):
        assert 0 < len(active["name"]) <= 120
        assert 0 < len(active["id"]) <= 120
    form_field = out["summary"]["dom_outline"]["forms"][0]["fields"][0]
    assert 0 < len(form_field["name"]) <= 120


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_ordinary_values_are_unchanged() -> None:
    out = _run_scripts()

    assert out["interactables"][0]["tag"] == "a"
    assert out["active"]["tag"] == "input"
