"""The observation scripts' own length cuts never leave half of an emoji (finding D3a-1).

abCap and abCapUrl back off a cut inside a surrogate pair; the element labels (120, 160 and 80 characters), the
heading text and the text excerpt were raw `.slice` calls, so a lone surrogate could reach the JSON encoder, which
refuses it. The scripts run under Node against minimal fake DOM elements, as in test_observation_string_caps.py.
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
EMOJI = "\U0001f600"

# `%(...)s` slots take script sources; `%(names)s` is a JSON object of the label text for each cut.
_RUNNER = r"""
const NAMES = %(names)s;
function fake(tag, attrs, extra) {
  return Object.assign({
    tagName: tag.toUpperCase(), innerText: '', textContent: '', value: '', labels: [], disabled: false,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    getBoundingClientRect: () => ({ x: 1, y: 2, width: 30, height: 10, top: 2 }),
    closest: () => null,
  }, extra);
}
const button = fake('button', { 'aria-label': NAMES.interactable });
const active = fake('input', { 'aria-label': NAMES.active, type: 'text' });
const field = fake('input', { 'aria-label': NAMES.field, type: 'text' });
const form = { getAttribute: () => null, querySelectorAll: () => [field] };
globalThis.window = { getComputedStyle: () => ({ visibility: 'visible', display: 'block' }) };
globalThis.document = {
  activeElement: active,
  title: 'title',
  body: { innerText: NAMES.body },
  forms: [form],
  getElementById: () => null,
  querySelectorAll: (sel) => {
    if (sel.startsWith('a,')) return [button];
    if (sel === 'h1,h2,h3') return [fake('h1', {}, { innerText: NAMES.heading })];
    return [];
  },
};
const interactables = (%(interactables)s)(10, null);
const activeOut = (%(active)s)(null, null);
const summary = (%(summary)s)(%(limit)d, null);
console.log(JSON.stringify({ interactables, activeOut, summary }));
"""


def _run(**names: str) -> dict:
    cuts = {
        "interactable": "ok",
        "active": "ok",
        "field": "ok",
        "heading": "ok",
        "body": "ok",
    }
    # The summary's text limit: the excerpt is the body cut at it.
    limit = 50
    source = _RUNNER % {
        "names": json.dumps({**cuts, **names}),
        "limit": limit,
        "interactables": browser_scripts.INTERACTABLES_SCRIPT,
        "active": browser_scripts.ACTIVE_ELEMENT_SCRIPT,
        "summary": browser_scripts.PAGE_SUMMARY_SCRIPT,
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


def _lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


needs_node = pytest.mark.skipif(NODE is None, reason="node not available")


@needs_node
def test_a_label_cut_inside_an_emoji_drops_the_emoji_instead_of_half_of_it() -> None:
    # The emoji's high surrogate is the last unit the cut would keep (the cap is 120, 160 or 80 units).
    out = _run(
        interactable="x" * 159 + EMOJI + "tail",
        active="y" * 119 + EMOJI + "tail",
        field="z" * 79 + EMOJI + "tail",
    )

    interactable = out["interactables"][0]["label"]
    active = out["activeOut"]["label"]
    field = out["summary"]["dom_outline"]["forms"][0]["fields"][0]["label"]
    assert (interactable, active, field) == ("x" * 159, "y" * 119, "z" * 79)
    for label in (interactable, active, field):
        assert not _lone_surrogate(label)
        label.encode("utf-8")


@needs_node
def test_the_text_excerpt_and_a_heading_cut_inside_an_emoji_drop_the_emoji() -> None:
    out = _run(body="b" * 49 + EMOJI + "tail", heading="h" * 159 + EMOJI + "tail")

    excerpt = out["summary"]["text_excerpt"]
    heading = out["summary"]["dom_outline"]["headings"][0]["text"]
    assert (excerpt, heading) == ("b" * 49, "h" * 159)


@needs_node
def test_a_label_that_fits_keeps_its_emoji() -> None:
    out = _run(interactable="go " + EMOJI, body="read " + EMOJI)

    assert out["interactables"][0]["label"] == "go " + EMOJI
    assert out["summary"]["text_excerpt"] == "read " + EMOJI
