"""Pages that work against an action named by element_id, from two reviews of the first fix.

The first fix kept a one-time marker attribute on the element and let
Playwright's locator find it again for its checks and some actions. The page
could see the marker arrive and leave, move it, or move the element itself, so
the page still chose what Playwright checked and acted on:

* a hidden "Pay now" over "Cancel" holding a `<label for="cancel">`, plus a
  script moving the marker to a decoy, turned a click on Cancel into a payment
  (the controller's own hit test accepted the label);
* moving the element into a closed shadow root or an iframe, and marking a decoy,
  sent an upload or a selection to the decoy;
* a script reacting to the marker's removal stole focus after the focus check,
  and a timer stole it during the clear;
* a disabled select swapped for a marked decoy while Playwright waited;
* selector hints still went to the live page in drag, find and wait;
* page scripts replaced the DOM primitives the controller reads.

Same harness as tests/test_element_ids.py: real BrowserManager, real Chromium,
a local server.
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.action_errors import BrowserActionError
from app.tool_gateway import McpToolGateway
from app.tool_inputs import DragDropInput, FindElementsInput, WaitForSelectorInput
from tests.test_element_ids import ID_FORMAT, _shared_chromium, browser_env, pytestmark  # noqa: F401

# Fable's probe page. "Pay now" is aria-hidden (observe never lists it), sits over
# Cancel, and holds a label for Cancel; the script moves any attribute set on Cancel
# onto a visible decoy.
LABEL_COVER = """<title>checkout</title><body style="margin:40px">
<script>window.log = [];</script>
<button id="cancel" style="display:block;width:200px;height:40px"
  onclick="window.log.push('cancel'); document.title='CANCELLED'">Cancel</button>
<div aria-hidden="true"><button id="pay"
  style="position:absolute;left:40px;top:40px;width:200px;height:40px;opacity:0;z-index:9;padding:0;border:0"
  onclick="window.log.push('pay'); document.title='PAID'; event.preventDefault()"
  ><label for="cancel" style="display:block;width:200px;height:40px"></label></button></div>
</body>"""
LABEL_COVER_MOVING_MARKER = LABEL_COVER.replace(
    "</body>",
    """<button id="decoy" style="display:block;margin-top:200px;width:200px;height:40px"
  onclick="window.log.push('decoy')">Decoy</button>
<script>
new MutationObserver((records) => {
  for (const record of records) {
    if (record.target.id !== 'cancel' || record.attributeName === 'style') continue;
    const value = record.target.getAttribute(record.attributeName);
    if (value === null) continue;
    record.target.removeAttribute(record.attributeName);
    document.getElementById('decoy').setAttribute(record.attributeName, value);
  }
}).observe(document, {subtree: true, attributes: true});
</script></body>""",
)


@pytest.mark.parametrize("page_html", [LABEL_COVER, LABEL_COVER_MOVING_MARKER], ids=["cover", "cover_moving_marker"])
async def test_a_label_over_the_element_never_takes_its_click(tmp_path: Path, page_html: str) -> None:
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id=cancel["element_id"])

        page = env.page(session_id)
        assert await page.evaluate("window.log") == []
        assert await page.title() == "checkout"
        assert raised.value.code == "element_covered"
        assert cancel["element_id"] in raised.value.message


# A transparent "Pay now" goes up over Cancel 40 ms after the pointer stops
# moving over it: after the pointer has arrived, during the pause before the press.
COVER_AFTER_ARRIVAL = """<title>checkout</title><body style="margin:40px">
<button id="cancel" style="display:block;width:200px;height:40px"
  onclick="document.title='CANCELLED'">Cancel</button>
<button id="pay" aria-hidden="true"
  style="display:none;position:absolute;left:40px;top:40px;width:200px;height:40px;opacity:0;z-index:9"
  onclick="document.title='PAID'">Pay now</button>
<script>
let timer = null;
document.getElementById('cancel').addEventListener('mousemove', () => {
  clearTimeout(timer);
  timer = setTimeout(() => { document.getElementById('pay').style.display = 'block'; }, 40);
});
</script>
</body>"""


async def test_a_cover_raised_after_the_pointer_arrives_takes_no_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hit test runs after the pause before the press, so a cover put up in the pause is seen."""
    # Every random draw at its midpoint: the pause before the press is 75 ms, the moves 11 ms apart.
    monkeypatch.setattr(random, "uniform", lambda low, high: (low + high) / 2)
    async with browser_env(tmp_path, {"/": COVER_AFTER_ARRIVAL}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id=cancel["element_id"])

        assert raised.value.code == "element_covered"
        assert "nothing was clicked" in raised.value.message
        assert await env.page(session_id).title() == "checkout"


# The page moves the element itself somewhere the light DOM does not reach once
# anything marks it, and puts the mark on a decoy of its own.
RELOCATES = """<title>relocate</title>
<input id="file" type="file" aria-label="Attachment" onchange="document.title='FILE ' + this.files[0].name">
<input id="decoy-file" type="file" aria-label="Decoy file" onchange="document.title='DECOY ' + this.files[0].name">
<select id="size" aria-label="Size"><option value="s">small</option><option value="l">large</option></select>
<select id="decoy-size" aria-label="Decoy size"><option value="s">small</option><option value="l">large</option></select>
<div id="host"></div>
<iframe id="frame" srcdoc="<body></body>"></iframe>
<script>
const decoys = { file: 'decoy-file', size: 'decoy-size' };
const where = location.pathname.slice(1);
let shadow = null;
new MutationObserver((records) => {
  for (const record of records) {
    const target = record.target;
    const value = target.getAttribute(record.attributeName);
    if (!decoys[target.id] || value === null || record.attributeName === 'style') continue;
    if (where === 'shadow') {
      shadow = shadow || document.getElementById('host').attachShadow({ mode: 'closed' });
      shadow.appendChild(target);
    } else {
      document.getElementById('frame').contentDocument.body.appendChild(target);
    }
    document.getElementById(decoys[target.id]).setAttribute(record.attributeName, value);
  }
}).observe(document, { subtree: true, attributes: true });
</script>
"""


@pytest.mark.parametrize("where", ["shadow", "iframe"])
async def test_an_upload_does_not_follow_a_relocated_element_to_a_decoy(tmp_path: Path, where: str) -> None:
    async with browser_env(tmp_path, {f"/{where}": RELOCATES}) as env:
        session_id = await env.create(f"/{where}")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        attachment = await env.element(session_id, "Attachment")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.upload(
                session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
            )

        page = env.page(session_id)
        assert await page.evaluate("document.getElementById('decoy-file').files.length") == 0
        assert not (await page.title()).startswith("DECOY")
        # Moved into the frame, the input is another document's, which the registry refuses as stale
        # before the upload's own check (round 4d); moved into a closed shadow root, it is still this one's.
        assert raised.value.code == ("stale_element_id" if where == "iframe" else "element_target_moved")


# The page copies any attribute set on Attachment onto a file input inside an
# open shadow root placed before it: the document's querySelectorAll does not see
# that copy, Playwright's locator does.
COPIES_INTO_AN_OPEN_SHADOW_ROOT = """<title>upload</title>
<div id="host"></div>
<input id="file" type="file" aria-label="Attachment">
<script>
const root = document.getElementById('host').attachShadow({ mode: 'open' });
root.innerHTML = '<input id="decoy" type="file">';
const field = document.getElementById('file');
new MutationObserver((records) => {
  for (const record of records) {
    const value = field.getAttribute(record.attributeName);
    if (value !== null && record.attributeName !== 'style') root.getElementById('decoy').setAttribute(record.attributeName, value);
  }
}).observe(field, { attributes: true });
</script>
"""


async def test_an_upload_is_refused_when_the_marker_is_copied_into_an_open_shadow_root(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": COPIES_INTO_AN_OPEN_SHADOW_ROOT}) as env:
        session_id = await env.create("/")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        attachment = await env.element(session_id, "Attachment")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.upload(
                session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
            )

        # Playwright's strict resolution refuses the second marked node (a strict-mode violation); Opus 10:
        # that is the page copying the marker, not a failure a retry would get past.
        assert raised.value.code == "element_target_moved" and raised.value.retryable is False
        page = env.page(session_id)
        decoy = "document.getElementById('host').shadowRoot.getElementById('decoy')"
        assert await page.evaluate(f"{decoy}.files.length") == 0
        assert await page.evaluate("document.getElementById('file').files.length") == 0
        # The marker is removed from the copy in the shadow root too (Codex 8).
        assert await page.evaluate(f"{decoy}.hasAttribute('data-auto-browser-target')") is False


@pytest.mark.parametrize("where", ["shadow", "iframe"])
async def test_a_selection_does_not_follow_a_relocated_element_to_a_decoy(tmp_path: Path, where: str) -> None:
    async with browser_env(tmp_path, {f"/{where}": RELOCATES}) as env:
        session_id = await env.create(f"/{where}")
        size = await env.element(session_id, "Size")

        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")

        page = env.page(session_id)
        assert await page.input_value("#decoy-size") == "s"
        assert await page.input_value("#size") == "l"


# Focus moves to a decoy whenever anything changes an attribute of the field.
STEALS_FOCUS_ON_ATTRIBUTES = """<title>form</title>
<input id="name" aria-label="Name">
<input id="other" aria-label="Other">
<script>
const field = document.getElementById('name');
new MutationObserver((records) => {
  if (records.some((record) => record.target === field && record.attributeName !== 'style')) {
    document.getElementById('other').focus();
  }
}).observe(document, { subtree: true, attributes: true });
</script>
"""

# A timer moves focus to a decoy once the clear's Delete has gone in.
STEALS_FOCUS_DURING_THE_CLEAR = """<title>form</title>
<input id="name" aria-label="Name" value="old">
<input id="other" aria-label="Other">
<script>
document.getElementById('name').addEventListener('keydown', (event) => {
  if (event.key === 'Delete') setTimeout(() => document.getElementById('other').focus(), 0);
});
</script>
"""


async def test_a_page_reacting_to_attribute_changes_cannot_steal_the_keystrokes(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": STEALS_FOCUS_ON_ATTRIBUTES}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        await env.manager.type(session_id, element_id=name["element_id"], text="hello")

        page = env.page(session_id)
        assert [await page.input_value("#name"), await page.input_value("#other")] == ["hello", ""]


async def test_focus_stolen_during_the_clear_stops_the_typing(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": STEALS_FOCUS_DURING_THE_CLEAR}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.type(session_id, element_id=name["element_id"], text="hunter2")

        page = env.page(session_id)
        assert await page.input_value("#other") == ""
        assert raised.value.code == "focus_lost"


# A disabled select the page replaces, a moment later, with an enabled decoy that
# carries every attribute the original had then (a marker included).
SWAPS_A_DISABLED_SELECT = """<title>shop</title>
<select id="size" aria-label="Size" disabled><option value="s">small</option><option value="l">large</option></select>
<script>
let swapAt = 0;
window.arm = (delay) => {
  const at = performance.now() + delay;
  swapAt = swapAt ? Math.min(swapAt, at) : at;
};
new MutationObserver((records) => {
  if (records.some((record) => record.target.id === 'size' && record.attributeName !== 'style')) window.arm(300);
}).observe(document, { subtree: true, attributes: true });
setInterval(() => {
  if (!swapAt || performance.now() < swapAt) return;
  swapAt = 0;
  const original = document.getElementById('size');
  if (!original) return;
  const decoy = document.createElement('select');
  decoy.innerHTML = original.innerHTML;
  for (const attribute of original.attributes) {
    if (!['id', 'disabled'].includes(attribute.name)) decoy.setAttribute(attribute.name, attribute.value);
  }
  decoy.id = 'decoy';
  decoy.onchange = () => { document.title = 'DECOY ' + decoy.value; };
  original.replaceWith(decoy);
}, 50);
</script>
"""


async def test_a_disabled_select_swapped_for_a_decoy_is_never_selected(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": SWAPS_A_DISABLED_SELECT}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")
        page = env.page(session_id)
        await page.evaluate("window.arm(1000)")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.select_option(session_id, element_id=size["element_id"], value="l")

        assert not (await page.title()).startswith("DECOY")
        assert await page.evaluate("document.getElementById('decoy')?.value ?? 's'") == "s"
        assert raised.value.code == "stale_element_id"


# Pointer events are logged by the element they land on; the page writes the real
# ids of Source and Target onto two elements of its own, placed first.
DRAG = """<title>drag</title>
<div id="c" style="width:80px;height:40px">c</div>
<div id="d" style="width:80px;height:40px">d</div>
<button id="a" style="display:block;width:80px;height:40px">Source</button>
<button id="b" style="display:block;width:80px;height:40px;margin-top:60px">Target</button>
<script>
window.log = [];
document.addEventListener('mousedown', (event) => window.log.push('down:' + event.target.id));
document.addEventListener('mouseup', (event) => window.log.push('up:' + event.target.id));
</script>
"""


def _gateway(env) -> McpToolGateway:
    return McpToolGateway(manager=env.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())


async def test_a_drag_between_selector_hints_uses_the_observed_elements(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": DRAG}) as env:
        session_id = await env.create("/")
        source = await env.element(session_id, "Source")
        target = await env.element(session_id, "Target")
        page = env.page(session_id)
        await page.evaluate(
            """([source, target]) => {
              document.getElementById('c').setAttribute('data-operator-id', source);
              document.getElementById('d').setAttribute('data-operator-id', target);
            }""",
            [source["element_id"], target["element_id"]],
        )

        await _gateway(env)._drag_drop(
            DragDropInput(
                session_id=session_id, source_selector=source["selector_hint"], target_selector=target["selector_hint"]
            )
        )

        assert await page.evaluate("window.log") == ["down:a", "up:b"]


async def test_a_drag_from_an_unknown_id_is_refused(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": DRAG}) as env:
        session_id = await env.create("/")
        target = await env.element(session_id, "Target")

        with pytest.raises(BrowserActionError) as raised:
            await _gateway(env)._drag_drop(
                DragDropInput(
                    session_id=session_id,
                    source_selector='[data-operator-id="op-' + "0" * 24 + '"]',
                    target_selector=target["selector_hint"],
                )
            )

        assert raised.value.code == "unknown_element_id"
        assert await env.page(session_id).evaluate("window.log") == []


async def test_find_and_wait_refuse_a_selector_hint(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": DRAG}) as env:
        session_id = await env.create("/")
        source = await env.element(session_id, "Source")
        gateway = _gateway(env)

        with pytest.raises(BrowserActionError) as found:
            await gateway._find_elements(FindElementsInput(session_id=session_id, selector=source["selector_hint"]))
        with pytest.raises(BrowserActionError) as waited:
            await gateway._wait_for_selector(
                WaitForSelectorInput(
                    session_id=session_id, selector=source["selector_hint"], timeout_ms=500, state="attached"
                )
            )

        for raised in (found, waited):
            assert raised.value.code == "selector_hint_not_a_selector"
            assert source["element_id"] in raised.value.message and "element_id" in raised.value.message


# Page scripts replace what a main-world implementation would read: the hit test,
# boxes, focus, the maps and the random source.
PATCHED_PRIMITIVES = """<title>patched</title>
<button id="pay" onclick="document.title='PAID'">Pay now</button>
<button id="cancel" onclick="document.title='CANCELLED'">Cancel</button>
<input id="name" aria-label="Name">
<input id="other" aria-label="Other">
<script>
const pay = document.getElementById('pay');
const other = document.getElementById('other');
const box = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function () { return box.call(this.id === 'cancel' ? pay : this); };
Document.prototype.elementFromPoint = function () { return pay; };
const focus = HTMLElement.prototype.focus;
HTMLElement.prototype.focus = function () { focus.call(other); };
crypto.getRandomValues = (array) => array.fill(7);
Map.prototype.get = function () { return undefined; };
Map.prototype.set = function () { return this; };
WeakMap.prototype.get = function () { return undefined; };
WeakMap.prototype.set = function () { return this; };
</script>
"""


async def test_page_scripts_replacing_dom_primitives_change_nothing_an_id_action_does(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": PATCHED_PRIMITIVES}) as env:
        session_id = await env.create("/")
        items = await env.observe(session_id)
        ids = [item["element_id"] for item in items]
        cancel = next(item for item in items if item["label"] == "Cancel")
        name = next(item for item in items if item["label"] == "Name")

        await env.manager.click(session_id, element_id=cancel["element_id"])
        await env.manager.type(session_id, element_id=name["element_id"], text="hello")

        page = env.page(session_id)
        assert len(ids) == 4 and len(set(ids)) == 4 and all(ID_FORMAT.fullmatch(i) for i in ids), ids
        assert await page.title() == "CANCELLED"
        assert [await page.input_value("#name"), await page.input_value("#other")] == ["hello", ""]
