"""What an action named by element_id does on honest pages, now that it never asks the page where its element is.

Click, hover, type and select run on the registered element in the isolated
world: they scroll it into view, wait (up to the action timeout) until it is
visible, stable and, where that matters, enabled, and refuse with a code that
says why when it never is. Upload is the one action Playwright still performs,
on the element it resolves once through a one-time marker, and it checks
afterwards that the file reached the registered element.

Same harness as tests/test_element_ids.py: real BrowserManager, real Chromium,
a local server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.action_errors import BrowserActionError
from tests.test_element_ids import TWO_BUTTONS, _shared_chromium, browser_env, pytestmark  # noqa: F401


async def _refusal(call) -> BrowserActionError:
    with pytest.raises(BrowserActionError) as raised:
        await call
    return raised.value


BUTTONS = """<title>buttons</title><body style="margin:40px">
<button id="go" onclick="document.title='GO'">Go</button>
<button id="later" style="display:none" onclick="document.title='LATER'">Later</button>
</body>"""


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("el.disabled = true", "element_disabled"),
        ("el.setAttribute('aria-disabled', 'true')", "element_disabled"),
        ("el.style.visibility = 'hidden'", "element_not_visible"),
        ("el.style.display = 'none'", "element_not_visible"),
        (
            "el.animate([{ transform: 'translateX(0)' }, { transform: 'translateX(300px)' }],"
            " { duration: 400, iterations: Infinity })",
            "element_not_stable",
        ),
    ],
)
async def test_a_click_on_an_element_that_never_becomes_actionable_is_refused(
    tmp_path: Path, change: str, code: str
) -> None:
    async with browser_env(tmp_path, {"/": BUTTONS}) as env:
        session_id = await env.create("/")
        go = await env.element(session_id, "Go")
        page = env.page(session_id)
        await page.evaluate(f"() => {{ const el = document.getElementById('go'); {change}; }}")

        refused = await _refusal(env.manager.click(session_id, element_id=go["element_id"]))

        assert refused.code == code
        assert go["element_id"] in refused.message
        assert await page.title() == "buttons"


async def test_a_click_waits_for_an_element_that_becomes_actionable(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": BUTTONS}) as env:
        session_id = await env.create("/")
        go = await env.element(session_id, "Go")
        page = env.page(session_id)
        await page.evaluate(
            "() => { const el = document.getElementById('go'); el.disabled = true;"
            " setTimeout(() => { el.disabled = false; }, 600); }"
        )

        await env.manager.click(session_id, element_id=go["element_id"])

        assert await page.title() == "GO"


# The pointer at Go's centre hits an icon inside it, drawn inside a shadow root.
NESTED = """<title>nested</title><body style="margin:40px">
<button id="go" style="width:120px;height:40px;padding:0" onclick="document.title='GO'"
  ><x-icon style="display:block;width:120px;height:40px"></x-icon></button>
<script>
customElements.define('x-icon', class extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'closed' }).innerHTML = '<span style="display:block;width:120px;height:40px">Go</span>';
  }
});
</script>
</body>"""


async def test_a_click_on_an_element_whose_centre_is_its_own_content_lands(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": NESTED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        go = [item for item in await env.observe(session_id) if item["tag"] == "button"][0]

        await env.manager.click(session_id, element_id=go["element_id"])

        assert await page.title() == "GO"


async def test_a_hover_on_an_element_with_no_box_is_refused(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": BUTTONS}) as env:
        session_id = await env.create("/")
        go = await env.element(session_id, "Go")
        await env.page(session_id).evaluate("document.getElementById('go').style.display = 'none'")

        refused = await _refusal(env.manager.hover(session_id, element_id=go["element_id"]))

        assert refused.code == "element_not_visible"


TRAPPED_FIELD = """<!doctype html><body style="margin:40px">
<input id="target" aria-label="Target" style="width:300px;height:30px">
<input id="trap" style="position:absolute;left:40px;top:40px;width:300px;height:30px;opacity:0;z-index:9">
</body>"""


async def test_text_for_a_covered_field_goes_into_that_field_or_nowhere(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TRAPPED_FIELD}) as env:
        session_id = await env.create("/")
        target = await env.element(session_id, "Target")

        try:
            await env.manager.type(session_id, element_id=target["element_id"], text="hunter2")
        except BrowserActionError as exc:
            assert exc.code == "focus_lost"

        page = env.page(session_id)
        assert await page.input_value("#trap") == ""
        assert await page.input_value("#target") in {"", "hunter2"}


# Name hands focus on to Other whenever it gets it.
HANDS_FOCUS_ON = """<title>form</title>
<input id="name" aria-label="Name" onfocus="document.getElementById('other').focus()">
<input id="other" aria-label="Other">
"""


@pytest.mark.parametrize("clear_first", [True, False])
async def test_a_field_that_hands_focus_on_gets_no_keystrokes_anywhere(tmp_path: Path, clear_first: bool) -> None:
    async with browser_env(tmp_path, {"/": HANDS_FOCUS_ON}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        refused = await _refusal(
            env.manager.type(session_id, element_id=name["element_id"], text="hunter2", clear_first=clear_first)
        )

        assert refused.code == "focus_lost"
        page = env.page(session_id)
        assert [await page.input_value("#name"), await page.input_value("#other")] == ["", ""]


SELECTS = """<title>selects</title>
<select id="size" aria-label="Size">
  <option value="s">small</option><option value="m">  medium   size </option><option value="l" disabled>large</option>
</select>
<select id="toppings" aria-label="Toppings" multiple>
  <option value="ham">Ham</option><option value="olive">Olive</option>
</select>
<button id="go">Go</button>
<script>
window.events = [];
for (const id of ['size', 'toppings']) {
  const el = document.getElementById(id);
  el.addEventListener('input', (event) => window.events.push(id + ':input:' + event.bubbles));
  el.addEventListener('change', (event) => window.events.push(id + ':change:' + event.bubbles));
}
</script>
"""


async def test_select_by_value_label_and_index_with_playwrights_rules(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": SELECTS}) as env:
        session_id = await env.create("/")
        size = (await env.element(session_id, "Size"))["element_id"]
        toppings = (await env.element(session_id, "Toppings"))["element_id"]
        page = env.page(session_id)

        await env.manager.select_option(session_id, element_id=size, label="medium size")
        assert await page.input_value("#size") == "m"
        await env.manager.select_option(session_id, element_id=size, index=0)
        assert await page.input_value("#size") == "s"
        # A value matches an option's label too, as Playwright's valueOrLabel does.
        await env.manager.select_option(session_id, element_id=size, value="medium size")
        assert await page.input_value("#size") == "m"
        await env.manager.select_option(session_id, element_id=toppings, value="olive")
        assert await page.evaluate("[...document.getElementById('toppings').selectedOptions].map((o) => o.value)") == [
            "olive"
        ]

        assert await page.evaluate("window.events") == [
            "size:input:true",
            "size:change:true",
            "size:input:true",
            "size:change:true",
            "size:input:true",
            "size:change:true",
            "toppings:input:true",
            "toppings:change:true",
        ]


@pytest.mark.parametrize(
    ("label", "kwargs", "code"),
    [
        ("Size", {"value": "xl"}, "option_not_found"),
        ("Size", {"value": "l"}, "option_disabled"),
        ("Go", {"value": "s"}, "element_not_a_select"),
    ],
)
async def test_a_selection_that_cannot_be_made_is_refused(tmp_path: Path, label: str, kwargs: dict, code: str) -> None:
    async with browser_env(tmp_path, {"/": SELECTS}) as env:
        session_id = await env.create("/")
        element = await env.element(session_id, label)
        page = env.page(session_id)

        refused = await _refusal(env.manager.select_option(session_id, element_id=element["element_id"], **kwargs))

        assert refused.code == code
        assert await page.input_value("#size") == "s"
        assert await page.evaluate("window.events") == []


async def test_a_disabled_select_is_refused(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": SELECTS}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")
        page = env.page(session_id)
        await page.evaluate("document.getElementById('size').disabled = true")

        refused = await _refusal(env.manager.select_option(session_id, element_id=size["element_id"], value="m"))

        assert refused.code == "element_disabled"
        assert await page.input_value("#size") == "s"


UPLOADS = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment" onchange="document.title='FILE ' + this.files[0].name">
<input id="decoy" type="file" aria-label="Decoy" onchange="document.title='DECOY ' + this.files[0].name">
"""


# (An upload a page redirects after the checks is recorded, not raised:
# tests/test_element_id_delivery.py.)


# Custom controls: a focusable label stands in for a hidden file input, or names a
# select. Playwright's file transfer and selection follow a label to its control
# ('follow-label'), as a person's click on it does, and 1.11.0 got that through
# Playwright; so do these by element_id.
LABELS_FOR_CONTROLS = """<title>labels</title><body style="margin:40px">
<label tabindex="0" for="file">Attach file</label>
<input id="file" type="file" style="display:none" onchange="document.title='FILE ' + this.files[0].name">
<label tabindex="0" for="size">Size</label>
<select id="size" aria-label="Size choice"><option value="s">small</option><option value="l">large</option></select>
<label tabindex="0" for="note">Note</label><input id="note" aria-label="Note field">
<label tabindex="0">Wrapped <select id="wrapped" aria-label="Wrapped choice">
  <option value="a">a</option><option value="b" disabled>b</option></select></label>
</body>"""


async def test_an_upload_by_a_labels_element_id_goes_to_the_file_input_it_is_for(tmp_path: Path) -> None:
    """Ledger C4: the release refused a label (element_not_a_file_input), where 1.11.0 followed it."""
    async with browser_env(tmp_path, {"/": LABELS_FOR_CONTROLS}) as env:
        session_id = await env.create("/")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        attach = await env.element(session_id, "Attach file")

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attach["element_id"]
        )

        page = env.page(session_id)
        assert await page.title() == "FILE report.txt"
        assert result["target"]["delivery"]["outcome"] == "confirmed"
        assert await page.evaluate("document.querySelectorAll('[data-auto-browser-target]').length") == 0


async def test_an_upload_by_a_label_is_confirmed_from_its_input_when_the_page_withholds_its_events(
    tmp_path: Path,
) -> None:
    """With no input or change event to read, the confirmation reads the file input the label stood for."""
    withholds = LABELS_FOR_CONTROLS.replace(
        "</body>",
        "<script>for (const type of ['input', 'change']) "
        "addEventListener(type, (event) => event.stopImmediatePropagation(), true);</script></body>",
    )
    async with browser_env(tmp_path, {"/": withholds}) as env:
        session_id = await env.create("/")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        attach = await env.element(session_id, "Attach file")

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attach["element_id"]
        )

        assert await env.page(session_id).evaluate("document.getElementById('file').files[0].name") == "report.txt"
        assert result["target"]["delivery"]["outcome"] == "confirmed"


async def test_a_selection_by_a_labels_element_id_is_made_in_the_select_it_is_for(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": LABELS_FOR_CONTROLS}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")

        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")

        assert await env.page(session_id).input_value("#size") == "l"


async def test_an_upload_by_a_label_for_another_kind_of_control_is_refused(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": LABELS_FOR_CONTROLS}) as env:
        session_id = await env.create("/")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        note = await env.element(session_id, "Note")

        refused = await _refusal(
            env.manager.upload(session_id, file_path="report.txt", approved=False, element_id=note["element_id"])
        )

        assert refused.code == "element_not_a_file_input"
        assert await env.page(session_id).title() == "labels"


async def test_a_disabled_option_in_a_select_a_label_wraps_is_refused(tmp_path: Path) -> None:
    """An option is checked as itself: following a label from it reached the select, which is enabled."""
    async with browser_env(tmp_path, {"/": LABELS_FOR_CONTROLS}) as env:
        session_id = await env.create("/")
        wrapped = await env.element(session_id, "Wrapped choice")

        refused = await _refusal(env.manager.select_option(session_id, element_id=wrapped["element_id"], value="b"))

        assert refused.code == "option_disabled"
        assert await env.page(session_id).input_value("#wrapped") == "a"


# A waits disabled while the label names it; B is enabled. A same-origin frame
# waits below, for a control the page adopts into it.
CHANGES_DURING_THE_WAIT = """<title>waits</title><body style="margin:40px">
<label tabindex="0" for="a">Size</label>
<select id="a" aria-label="A" disabled><option value="s">small</option><option value="l">large</option></select>
<select id="b" aria-label="B"><option value="s">small</option><option value="l">large</option></select>
<button id="go" disabled onclick="document.title='GO'">Go</button>
<iframe id="frame" srcdoc="<body></body>" style="width:300px;height:200px"></iframe>
</body>"""

ADOPT_AND_ENABLE = """(id) => {
  const el = document.getElementById(id);
  document.getElementById('frame').contentDocument.body.appendChild(el);
  el.disabled = false;
}"""


async def _while_it_waits(env, session_id: str, call, change: str, arg=None) -> BrowserActionError:
    """Start the action, let it wait on the disabled control, change the page, then take the refusal."""
    task = asyncio.ensure_future(call)
    await asyncio.sleep(0.5)
    assert not task.done(), "the action did not wait"
    await env.page(session_id).evaluate(change, arg)
    return await _refusal(task)


async def test_a_selection_checks_the_select_it_changes_when_the_label_is_pointed_elsewhere(tmp_path: Path) -> None:
    """Codex, round 4f: the wait followed the label's current control for the enabled check, but the selection
    was made in the control found first, so a disabled select was changed once the label named an enabled one."""
    async with browser_env(tmp_path, {"/": CHANGES_DURING_THE_WAIT}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")

        refused = await _while_it_waits(
            env,
            session_id,
            env.manager.select_option(session_id, element_id=size["element_id"], value="l"),
            "() => { document.querySelector('label').htmlFor = 'b'; }",
        )

        page = env.page(session_id)
        assert refused.code == "element_disabled"
        assert [await page.input_value("#a"), await page.input_value("#b")] == ["s", "s"]


@pytest.mark.parametrize("action", ["select", "click"])
async def test_a_control_the_page_adopts_into_a_frame_during_the_wait_is_refused_as_stale(
    tmp_path: Path, action: str
) -> None:
    """Codex, round 4f: the waits checked only that the element was attached, so a control a same-origin frame
    adopted while they waited was still acted on, its box the frame's."""
    async with browser_env(tmp_path, {"/": CHANGES_DURING_THE_WAIT}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        await page.wait_for_function("document.getElementById('frame').contentDocument.readyState === 'complete'")
        if action == "select":
            target = await env.element(session_id, "A")
            call = env.manager.select_option(session_id, element_id=target["element_id"], value="l")
            adopted = "a"
        else:
            target = await env.element(session_id, "Go")
            call = env.manager.click(session_id, element_id=target["element_id"])
            adopted = "go"

        refused = await _while_it_waits(env, session_id, call, ADOPT_AND_ENABLE, adopted)

        assert refused.code == "stale_element_id"
        assert await page.title() == "waits"
        if action == "select":
            value = "document.getElementById('frame').contentDocument.getElementById('a').value"
            assert await page.evaluate(value) == "s"


# Many upload widgets read the file on change and empty the input for the next one.
UPLOAD_RESETS_ON_CHANGE = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment"
  onchange="document.title='FILE ' + this.files[0].name; this.value = ''">
"""


async def test_an_upload_to_a_field_the_page_resets_on_change_still_succeeds(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": UPLOAD_RESETS_ON_CHANGE}) as env:
        session_id = await env.create("/")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        attachment = await env.element(session_id, "Attachment")

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        assert await env.page(session_id).title() == "FILE report.txt"
        assert result["target"]["delivery"]["outcome"] == "confirmed"


# "detached": on change the page takes the field out of the document and keeps it.
# "copied": the page copies any attribute set on the field onto Decoy.
UPLOAD_MARKER_LEFTOVERS = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment">
<input id="decoy" type="file" aria-label="Decoy">
<script>
const field = document.getElementById('file');
window.kept = field;
if (location.pathname === '/detached') field.addEventListener('change', () => field.remove());
if (location.pathname === '/copied') {
  new MutationObserver((records) => {
    for (const record of records) {
      const value = field.getAttribute(record.attributeName);
      if (record.target === field && value !== null) document.getElementById('decoy').setAttribute(record.attributeName, value);
    }
  }).observe(field, { attributes: true });
}
</script>
"""


@pytest.mark.parametrize("page_does", ["detached", "copied"])
async def test_the_upload_marker_is_removed_wherever_it_ended_up(tmp_path: Path, page_does: str) -> None:
    async with browser_env(tmp_path, {f"/{page_does}": UPLOAD_MARKER_LEFTOVERS}) as env:
        session_id = await env.create(f"/{page_does}")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        attachment = await env.element(session_id, "Attachment")
        page = env.page(session_id)

        upload = env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )
        if page_does == "copied":
            assert (await _refusal(upload)).code == "element_target_moved"
            assert await page.evaluate("document.getElementById('decoy').files.length") == 0
        else:
            result = await upload
            assert await page.evaluate("window.kept.isConnected") is False
            # The post-check reads the element the page took out of the document.
            assert result["target"]["delivery"]["outcome"] == "confirmed"

        assert await page.evaluate("window.kept.hasAttribute('data-auto-browser-target')") is False
        assert await page.locator("[data-auto-browser-target]").count() == 0


FORM = """<title>form</title>
<button id="go" onclick="document.title='GO'">Go</button>
<input id="name" aria-label="Name">
<select id="size" aria-label="Size"><option value="s">small</option><option value="l">large</option></select>
<input id="file" type="file" aria-label="Attachment">
"""


@pytest.mark.parametrize(
    ("action", "label", "dom_id"),
    [
        ("click", "Go", "go"),
        ("hover", "Go", "go"),
        ("type", "Name", "name"),
        ("select_option", "Size", "size"),
        ("upload", "Attachment", "file"),
    ],
)
async def test_every_action_refuses_an_element_the_page_removed(
    tmp_path: Path, action: str, label: str, dom_id: str
) -> None:
    async with browser_env(tmp_path, {"/": FORM}) as env:
        session_id = await env.create("/")
        (Path(env.manager.settings.upload_root) / "report.txt").write_text("quarterly", encoding="utf-8")
        element_id = (await env.element(session_id, label))["element_id"]
        page = env.page(session_id)
        await page.evaluate("(id) => { window.kept = document.getElementById(id); window.kept.remove(); }", dom_id)
        arguments = {"type": {"text": "x"}, "select_option": {"value": "l"}}.get(action, {})
        if action == "upload":
            arguments = {"file_path": "report.txt", "approved": False}

        refused = await _refusal(getattr(env.manager, action)(session_id, element_id=element_id, **arguments))

        assert refused.code == "stale_element_id"
        assert "no longer on the page" in refused.message
        assert await page.evaluate("window.kept.hasAttribute('data-auto-browser-target')") is False


async def test_an_id_from_the_page_before_going_back_is_refused(tmp_path: Path) -> None:
    """Going back loads a new document here (no back/forward cache restore), so its ids are new."""
    async with browser_env(tmp_path, {"/": TWO_BUTTONS, "/next": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        await env.manager.navigate(session_id, f"{env.origin}/next")
        await env.manager.go_back(session_id)

        refused = await _refusal(env.manager.click(session_id, element_id=cancel["element_id"]))

        assert refused.code == "stale_element_id"
        assert "earlier document" in refused.message
