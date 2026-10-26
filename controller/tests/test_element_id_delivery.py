"""What reaches the page when an action by element_id runs: round 3 of the reviews.

The clear before typing is checked between select-all and delete. An upload,
once Playwright has set the files, never fails: whether the registered input
holds the file is recorded as confirmed or not_confirmed (best-effort: the page
can forge it). A select is scrolled into view first. A world call that may have
changed the page is not sent twice, even when its answer was lost after it ran.
A page element named like the registry does not stand in for it.

Same harness as tests/test_element_ids.py: real BrowserManager, real Chromium,
a local server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import CDPSession, ElementHandle, Locator
from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.browser import element_world
from app.browser.element_target import ElementTarget
from tests.test_element_ids import _shared_chromium, browser_env, pytestmark  # noqa: F401

REPORT = "quarterly"  # 9 bytes


async def _refusal(call) -> BrowserActionError:
    with pytest.raises(BrowserActionError) as raised:
        await call
    return raised.value


def _stage(env) -> None:
    (Path(env.manager.settings.upload_root) / "report.txt").write_text(REPORT, encoding="utf-8")


# ── The clear ────────────────────────────────────────────────────────────────

# Select-all in Name moves focus to a filled decoy and selects its text.
SELECT_ALL_MOVES_FOCUS = """<title>form</title>
<input id="name" aria-label="Name" value="old">
<input id="decoy" aria-label="Decoy" value="keep me">
<script>
document.getElementById('name').addEventListener('keydown', (event) => {
  if (!event.ctrlKey || event.key.toLowerCase() !== 'a') return;
  event.preventDefault();
  const decoy = document.getElementById('decoy');
  decoy.focus();
  decoy.select();
});
</script>
"""


async def test_a_select_all_that_moves_focus_empties_nothing(tmp_path: Path) -> None:
    """Codex 2: focus was checked only after Delete, which had emptied the field the page chose."""
    async with browser_env(tmp_path, {"/": SELECT_ALL_MOVES_FOCUS}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        refused = await _refusal(env.manager.type(session_id, element_id=name["element_id"], text="new"))

        assert refused.code == "focus_lost"
        page = env.page(session_id)
        assert [await page.input_value("#name"), await page.input_value("#decoy")] == ["old", "keep me"]


# Opus's probe page: the same move, made by the Delete key's own keydown handler.
DELETE_MOVES_FOCUS = """<title>form</title>
<input id="name" aria-label="Name" value="old">
<input id="decoy" aria-label="Decoy" value="keep me">
<script>
document.getElementById('name').addEventListener('keydown', (event) => {
  if (event.key !== 'Delete') return;
  const decoy = document.getElementById('decoy');
  decoy.focus();
  decoy.select();
});
</script>
"""


async def test_a_delete_that_moves_focus_is_the_documented_limit_of_the_clear(tmp_path: Path) -> None:
    """Opus 2: a keydown handler can move focus within the key it handles, the clear's Delete included.

    Chromium then applies the Delete to the field the handler moved to, so that field is emptied; the
    check after the clear refuses before any text goes out. Documented in the residual lists (CHANGELOG,
    docs/llm-adapters.md, the advisory); if this changes, change them too.
    """
    async with browser_env(tmp_path, {"/": DELETE_MOVES_FOCUS}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        refused = await _refusal(env.manager.type(session_id, element_id=name["element_id"], text="new"))

        assert refused.code == "focus_lost"
        page = env.page(session_id)
        assert [await page.input_value("#name"), await page.input_value("#decoy")] == ["old", ""]


# Name hands focus to the decoy, which is in Name's form; Name is named
# "parentNode", so form.parentNode is Name, in the isolated world too, and a walk
# up from the focused decoy reached it.
HANDS_FOCUS_IN_ITS_FORM = """<title>form</title>
<form onsubmit="return false">
  <input name="parentNode" aria-label="Name">
  <input id="decoy" aria-label="Decoy">
</form>
<script>
document.querySelector('[name=parentNode]').addEventListener('focus', () => document.getElementById('decoy').focus());
</script>
"""


async def test_focus_on_a_decoy_in_a_form_with_a_shadowing_control_is_not_taken_for_the_element(
    tmp_path: Path,
) -> None:
    """Found in round 3: the focus check read node.parentNode, which a form's controls can shadow."""
    async with browser_env(tmp_path, {"/": HANDS_FOCUS_IN_ITS_FORM}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        refused = await _refusal(env.manager.type(session_id, element_id=name["element_id"], text="secret"))

        assert refused.code == "focus_lost"
        assert await env.page(session_id).input_value("#decoy") == ""


# Notes hands focus to a field in a frame inside it: the frame element is the
# document's active element, and it is inside Notes.
HANDS_FOCUS_TO_A_FRAME_INSIDE = """<title>notes</title><body style="margin:40px">
<div id="notes" contenteditable="true" aria-label="Notes" style="width:300px;height:120px;border:1px solid #999">old
<iframe id="inner" style="display:block;width:200px;height:30px;margin-top:70px"></iframe></div>
<script>
const doc = document.getElementById('inner').contentDocument;
doc.body.innerHTML = '<input id="secret">';
document.getElementById('notes').addEventListener('focus', () => doc.getElementById('secret').focus());
</script>
</body>"""


async def test_focus_in_a_frame_inside_the_element_is_not_focus_in_the_element(tmp_path: Path) -> None:
    """Opus 11: keys sent while a frame inside the element has focus go to the frame's document."""
    async with browser_env(tmp_path, {"/": HANDS_FOCUS_TO_A_FRAME_INSIDE}) as env:
        session_id = await env.create("/")
        notes = await env.element(session_id, "Notes")

        refused = await _refusal(env.manager.type(session_id, element_id=notes["element_id"], text="secret"))

        assert refused.code == "focus_lost"
        typed = "document.getElementById('inner').contentDocument.getElementById('secret').value"
        assert await env.page(session_id).evaluate(typed) == ""


# Field's open shadow root holds a frame, and focusing Field hands focus to a
# field in that frame: the document's active element is then Field itself (the
# frame is behind its shadow root); only the shadow root's names the frame.
HANDS_FOCUS_TO_A_FRAME_IN_ITS_SHADOW_ROOT = """<title>field</title><body style="margin:40px">
<div id="field" role="textbox" tabindex="0" aria-label="Field" style="display:block;width:300px;height:80px"></div>
<script>
const field = document.getElementById('field');
const root = field.attachShadow({ mode: 'open' });
root.innerHTML = '<iframe id="inner" style="display:block;width:300px;height:80px;border:0"></iframe>';
const doc = root.getElementById('inner').contentDocument;
doc.body.innerHTML = '<input id="secret">';
field.addEventListener('focus', () => doc.getElementById('secret').focus());
</script>
</body>"""


async def test_focus_in_a_frame_behind_the_elements_open_shadow_root_is_not_focus_in_the_element(
    tmp_path: Path,
) -> None:
    """Codex F5: the check read the document's active element, which is the shadow host when the focused
    frame is behind its open shadow root, so the keys went to the frame."""
    async with browser_env(tmp_path, {"/": HANDS_FOCUS_TO_A_FRAME_IN_ITS_SHADOW_ROOT}) as env:
        session_id = await env.create("/")
        field = await env.element(session_id, "Field")

        refused = await _refusal(env.manager.type(session_id, element_id=field["element_id"], text="secret"))

        assert refused.code == "focus_lost"
        typed = (
            "document.getElementById('field').shadowRoot.getElementById('inner')"
            ".contentDocument.getElementById('secret').value"
        )
        assert await env.page(session_id).evaluate(typed) == ""


async def test_focus_in_a_frame_behind_a_closed_shadow_root_is_the_documented_limit(tmp_path: Path) -> None:
    """The world cannot see into a closed shadow root, so focus in a frame behind the element's own closed
    root reads as focus on the element, and the keys go to the frame.

    Documented in the residual lists (CHANGELOG, docs/llm-adapters.md, the advisory). If this changes,
    change them too.
    """
    # The hit test sees only the host here, so the focus click lands in the frame; its click hands focus on.
    page_html = HANDS_FOCUS_TO_A_FRAME_IN_ITS_SHADOW_ROOT.replace("mode: 'open'", "mode: 'closed'").replace(
        "</script>",
        "doc.addEventListener('click', () => doc.getElementById('secret').focus());\n"
        "window.secretValue = () => doc.getElementById('secret').value;\n</script>",
    )
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        field = await env.element(session_id, "Field")

        await env.manager.type(session_id, element_id=field["element_id"], text="secret")

        assert await env.page(session_id).evaluate("window.secretValue()") == "secret"


async def test_type_by_selector_survives_a_page_that_removed_a_dom_accessor(tmp_path: Path) -> None:
    """Opus 14: in the main world the focus check needed every prototype getter, so selector typing broke."""
    page_html = """<title>form</title><input id="name" aria-label="Name">
<script>delete Element.prototype.shadowRoot;</script>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")

        await env.manager.type(session_id, selector="#name", text="hi")

        assert await env.page(session_id).input_value("#name") == "hi"


async def test_type_by_selector_reads_an_accessor_the_page_moved(tmp_path: Path) -> None:
    """Opus 14: the walk up from the focused field to the target reads parentNode even when the page moved
    the accessor off Node.prototype (here onto Element.prototype)."""
    page_html = """<title>form</title>
<label id="wrap" style="display:block;width:200px;padding:4px">Name <input id="name"></label>
<script>
const parentNode = Object.getOwnPropertyDescriptor(Node.prototype, 'parentNode');
delete Node.prototype.parentNode;
Object.defineProperty(Element.prototype, 'parentNode', parentNode);
</script>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")

        await env.manager.type(session_id, selector="#wrap", text="hi")

        assert await env.page(session_id).input_value("#name") == "hi"


# ── Upload: the outcome is recorded, never raised ────────────────────────────

UPLOADS = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment" onchange="document.title='FILE ' + this.files[0].name">
<input id="decoy" type="file" aria-label="Decoy" onchange="document.title='DECOY ' + this.files[0].name">
"""


async def test_an_honest_upload_is_confirmed(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": UPLOADS}) as env:
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        assert result["target"]["delivery"]["outcome"] == "confirmed"
        assert result["verification"]["verified"] is True
        assert "upload_confirmed" in result["verification"]["signals"]


# The decoy's change handler gives the registered input a file of its own with
# the uploaded file's name: 6 bytes ("forged") or the real file's 9 ("quarterly").
FORGES_THE_FILE = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment">
<input id="decoy" type="file" aria-label="Decoy" onchange="
  const dt = new DataTransfer();
  dt.items.add(new File([location.pathname === '/same-size' ? 'quarterly' : 'forged'], this.files[0].name));
  document.getElementById('file').files = dt.files;
  document.title = 'DECOY ' + this.files[0].name">
"""


@pytest.mark.parametrize(("path", "outcome"), [("/other-size", "not_confirmed"), ("/same-size", "confirmed")])
async def test_an_upload_the_page_redirected_is_recorded_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, outcome: str
) -> None:
    """Codex 3, Fable F1: the check is best-effort; a page that wins the race can make it pass."""
    async with browser_env(tmp_path, {path: FORGES_THE_FILE}) as env:
        session_id = await env.create(path)
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        page = env.page(session_id)

        async def resolves_the_decoy(self, **_kwargs):
            return await page.query_selector("#decoy")

        monkeypatch.setattr(Locator, "element_handle", resolves_the_decoy)
        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        assert await page.title() == "DECOY report.txt", "the decoy got the real file"
        assert result["target"]["delivery"]["outcome"] == outcome
        assert result["verification"]["verified"] is (outcome == "confirmed")


async def test_an_upload_that_lands_elsewhere_is_not_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with browser_env(tmp_path, {"/": UPLOADS}) as env:
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        page = env.page(session_id)

        async def resolves_the_decoy(self, **_kwargs):
            return await page.query_selector("#decoy")

        monkeypatch.setattr(Locator, "element_handle", resolves_the_decoy)
        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        delivery = result["target"]["delivery"]
        assert delivery["outcome"] == "not_confirmed" and delivery["reason"]
        assert "upload_not_confirmed" in result["verification"]["signals"]
        assert await page.evaluate("document.getElementById('file').files.length") == 0


async def test_the_change_listener_an_upload_adds_is_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 8: a marked input that never saw its change event kept the controller's listener."""
    async with browser_env(tmp_path, {"/": UPLOADS}) as env:
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        page = env.page(session_id)

        async def resolves_the_decoy(self, **_kwargs):
            return await page.query_selector("#decoy")

        monkeypatch.setattr(Locator, "element_handle", resolves_the_decoy)
        await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        cdp = await page.context.new_cdp_session(page)
        frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame, "worldName": element_world.WORLD_NAME})
        found = await cdp.send(
            "Runtime.evaluate",
            {"expression": "document.getElementById('file')", "contextId": world["executionContextId"]},
        )
        listeners = await cdp.send("DOMDebugger.getEventListeners", {"objectId": found["result"]["objectId"]})
        await cdp.detach()

        assert [entry for entry in listeners["listeners"] if entry["type"] in ("change", "input")] == []


# The page empties the field as it reads it: on input (a dropzone), on change in
# a capture listener on the window, or on input in one (which runs before the
# controller's listener on the field itself).
EMPTIES_THE_FIELD = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment">
<script>
const field = document.getElementById('file');
const empty = (event) => { if (event.target === field) field.value = ''; };
if (location.pathname === '/on-input') field.addEventListener('input', empty);
if (location.pathname === '/capture-change') window.addEventListener('change', empty, true);
if (location.pathname === '/capture-input') window.addEventListener('input', empty, true);
</script>
"""


@pytest.mark.parametrize(
    ("path", "outcome"),
    [("/on-input", "confirmed"), ("/capture-change", "confirmed"), ("/capture-input", "not_confirmed")],
)
async def test_an_upload_the_page_consumes_at_once_is_not_an_error(tmp_path: Path, path: str, outcome: str) -> None:
    """Codex 6, Opus 19: the files are recorded on input too, which a dropzone that empties the field on input
    used to turn into not_confirmed; only a capture listener on the window or document still runs first."""
    async with browser_env(tmp_path, {path: EMPTIES_THE_FIELD}) as env:
        session_id = await env.create(path)
        _stage(env)
        attachment = await env.element(session_id, "Attachment")

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        assert result["target"]["delivery"]["outcome"] == outcome


@pytest.mark.parametrize("by", ["element_id", "selector"])
async def test_an_upload_whose_page_closes_once_the_file_is_in_is_reported_and_its_approval_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    """Codex 1: a failure after the transfer (settling, observing) made the upload a retryable failure."""
    async with browser_env(tmp_path, {"/": UPLOADS}) as env:
        env.manager.settings.require_approval_for_uploads = True
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        target = {"element_id": attachment["element_id"]} if by == "element_id" else {"selector": "#file"}
        page = env.page(session_id)
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.upload(session_id, file_path="report.txt", approved=False, **target)
        approval = await env.manager.approvals.approve(asked.value.approval.id, comment="ok")
        owner = ElementHandle if by == "element_id" else Locator
        set_input_files = owner.set_input_files

        async def then_close(self, *args, **kwargs):
            await set_input_files(self, *args, **kwargs)
            await page.close()

        monkeypatch.setattr(owner, "set_input_files", then_close)

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=True, approval_id=approval.id, **target
        )

        assert "do not repeat the action" in result["target"]["after_change"], result["target"]
        if by == "element_id":
            assert result["target"]["delivery"]["outcome"] in ("confirmed", "not_confirmed")
        assert (await env.manager.approvals.get(approval.id)).status == "executed"


async def test_an_upload_cancelled_after_the_transfer_spends_its_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex F3: the transfer was recorded only once the whole element upload returned, so a cancellation
    during the delivery check left the approval to be executed again."""
    async with browser_env(tmp_path, {"/": UPLOADS}) as env:
        env.manager.settings.require_approval_for_uploads = True
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.upload(
                session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
            )
        approval = await env.manager.approvals.approve(asked.value.approval.id, comment="ok")
        transfers: list[object] = []
        set_input_files = ElementHandle.set_input_files

        async def counted(self, *args, **kwargs):
            transfers.append(args)
            await set_input_files(self, *args, **kwargs)

        checking = asyncio.Event()

        async def stuck(self, *args, **kwargs):
            checking.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(ElementHandle, "set_input_files", counted)
        monkeypatch.setattr(ElementTarget, "_delivery", stuck)
        upload = asyncio.ensure_future(
            env.manager.upload(
                session_id,
                file_path="report.txt",
                approved=True,
                approval_id=approval.id,
                element_id=attachment["element_id"],
            )
        )
        await asyncio.wait_for(checking.wait(), 10)
        upload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upload

        assert len(transfers) == 1
        assert (await env.manager.approvals.get(approval.id)).status == "executed"


# The one-time-code field hands focus on, so the TOTP fill that follows every action fails with focus_lost.
CODE_FIELD_HANDS_FOCUS_ON = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment">
<input id="code" autocomplete="one-time-code" aria-label="Code" onfocus="document.getElementById('other').focus()">
<input id="other" aria-label="Other">
"""


async def test_an_upload_whose_totp_fill_fails_afterwards_is_reported_not_raised(tmp_path: Path) -> None:
    """A retryable error after the transfer (the TOTP fill's focus_lost) made the upload a failure to retry."""
    async with browser_env(tmp_path, {"/": CODE_FIELD_HANDS_FOCUS_ON}) as env:
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        session = env.manager.sessions[session_id]
        session.totp_secret = "JBSWY3DPEHPK3PXP"
        session.totp_hosts = ("127.0.0.1",)

        result = await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )

        assert "Keyboard focus would not stay" in result["target"]["after_change"], result["target"]
        assert result["target"]["delivery"]["outcome"] == "confirmed"


CHALLENGE_ON_UPLOAD = """<title>uploads</title>
<input id="file" type="file" aria-label="Attachment" onchange="document.title='Solve the captcha'">
"""


async def test_an_upload_refused_after_the_file_is_in_still_spends_its_approval(tmp_path: Path) -> None:
    """A step after the transfer that raises (here a bot challenge) left the approval to be executed again."""
    async with browser_env(tmp_path, {"/": CHALLENGE_ON_UPLOAD}) as env:
        env.manager.settings.require_approval_for_uploads = True
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.upload(
                session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
            )
        approval = await env.manager.approvals.approve(asked.value.approval.id, comment="ok")

        refused = await _refusal(
            env.manager.upload(
                session_id,
                file_path="report.txt",
                approved=True,
                approval_id=approval.id,
                element_id=attachment["element_id"],
            )
        )

        assert refused.code == "captcha_detected"
        assert (await env.manager.approvals.get(approval.id)).status == "executed"


async def test_an_upload_whose_page_stops_answering_once_the_file_is_in_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the file has gone in nothing is raised, not even when the check and the cleanup get no answer."""
    async with browser_env(tmp_path, {"/": UPLOADS}) as env:
        session_id = await env.create("/")
        _stage(env)
        attachment = await env.element(session_id, "Attachment")
        page = env.page(session_id)
        world = env.manager.element_ids._world(page)
        send, set_input_files = world._send, ElementHandle.set_input_files
        stopped = False

        async def stops_once_the_file_is_in(cdp, method, params=None, timeout_ms=None):
            if not stopped:
                return await send(cdp, method, params, timeout_ms)
            return await world._within(asyncio.Event().wait(), method, timeout_ms or world.timeout_ms)

        async def then_stop(self, *args, **kwargs):
            nonlocal stopped
            await set_input_files(self, *args, **kwargs)
            stopped = True

        monkeypatch.setattr(world, "_send", stops_once_the_file_is_in)
        monkeypatch.setattr(ElementHandle, "set_input_files", then_stop)
        env.manager.settings.action_timeout_ms = 500

        result = await asyncio.wait_for(
            env.manager.upload(session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]),
            20,
        )

        assert await page.title() == "FILE report.txt"
        assert result["target"]["delivery"]["outcome"] == "not_confirmed"
        assert "could not run" in result["target"]["delivery"]["reason"]


# ── Select ───────────────────────────────────────────────────────────────────

# Late is far below the fold and gets its option only once it scrolls into view.
SELECT_BELOW_THE_FOLD = """<title>select</title>
<div style="height:3000px"></div>
<select id="late" aria-label="Late"><option value="">choose</option></select>
<script>
const late = document.getElementById('late');
new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting) && late.options.length === 1) late.add(new Option('Ready', 'ready'));
}).observe(late);
</script>
"""


async def test_a_select_is_scrolled_into_view_before_its_options_are_matched(tmp_path: Path) -> None:
    """Codex 4: the select path no longer scrolled, so options loaded on view never came."""
    async with browser_env(tmp_path, {"/": SELECT_BELOW_THE_FOLD}) as env:
        session_id = await env.create("/")
        late = await env.element(session_id, "Late")

        await env.manager.select_option(session_id, element_id=late["element_id"], value="ready")

        assert await env.page(session_id).input_value("#late") == "ready"


async def test_a_select_that_never_stops_moving_is_refused(tmp_path: Path) -> None:
    """The select's box must settle before options are matched, as for a click."""
    page_html = """<title>s</title>
<style>@keyframes slide { from { margin-left: 0 } to { margin-left: 200px } }</style>
<select id="size" aria-label="Size" style="animation: slide 0.5s linear infinite alternate">
<option value="s">small</option><option value="l">large</option></select>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")

        refused = await _refusal(env.manager.select_option(session_id, element_id=size["element_id"], value="l"))

        assert refused.code == "element_not_stable"
        assert await env.page(session_id).input_value("#size") == "s"


async def test_a_select_waits_for_an_option_that_arrives_after_the_call(tmp_path: Path) -> None:
    page_html = '<title>s</title><select id="size" aria-label="Size"><option value="s">small</option></select>'
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")
        await env.page(session_id).evaluate(
            "setTimeout(() => document.getElementById('size').add(new Option('large', 'l')), 800)"
        )

        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")

        assert await env.page(session_id).input_value("#size") == "l"


# ── Type into a contenteditable ──────────────────────────────────────────────


async def test_type_by_element_id_into_a_contenteditable(tmp_path: Path) -> None:
    page_html = """<title>notes</title><body style="margin:40px">
<div id="notes" contenteditable="true" aria-label="Notes" style="width:300px;height:60px;border:1px solid #999">old</div>
</body>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        notes = await env.element(session_id, "Notes")

        await env.manager.type(session_id, element_id=notes["element_id"], text="hello")

        assert await env.page(session_id).evaluate("document.getElementById('notes').textContent") == "hello"


# ── A world call whose answer is lost after it ran ───────────────────────────


def _lose_the_answer_once(monkeypatch: pytest.MonkeyPatch, fragment: str) -> dict:
    """Make the next world call whose function contains ``fragment`` run, then fail as a lost session."""
    state = {"armed": True}
    original = CDPSession.send

    async def send(self, method, params=None):
        reply = await original(self, method, params)
        declaration = (params or {}).get("functionDeclaration", "")
        if state["armed"] and method == "Runtime.callFunctionOn" and fragment in declaration:
            state["armed"] = False
            raise PlaywrightError("Target page, context or browser has been closed")
        return reply

    monkeypatch.setattr(CDPSession, "send", send)
    return state


async def test_a_selection_whose_answer_was_lost_is_not_made_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 7, Opus 9: never sent again, and the refusal says the change may have been made instead of
    inviting the retry that would make it twice."""
    page_html = """<title>s</title>
<select id="size" aria-label="Size"><option value="s">small</option><option value="l">large</option></select>
<script>window.changes = 0; document.getElementById('size').addEventListener('change', () => { window.changes += 1; });</script>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")
        state = _lose_the_answer_once(monkeypatch, "option_not_found")

        refused = await _refusal(env.manager.select_option(session_id, element_id=size["element_id"], value="l"))

        assert refused.code == "change_unconfirmed" and refused.retryable is False
        assert "may have been made" in refused.message and "retry" not in refused.message.lower()
        assert state["armed"] is False
        assert await env.page(session_id).evaluate("window.changes") == 1


@pytest.mark.parametrize("fragment", ["element_not_stable", "return ((el, x, y) =>"], ids=["wait", "hit_test"])
async def test_a_click_whose_read_lost_its_answer_presses_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """The wait (an idempotent scroll) and the hit test (a read) are sent again; the press is never repeated."""
    page_html = """<title>c</title><body style="margin:40px">
<button id="go" onclick="window.clicks = (window.clicks || 0) + 1">Go</button></body>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        go = await env.element(session_id, "Go")
        state = _lose_the_answer_once(monkeypatch, fragment)

        await env.manager.click(session_id, element_id=go["element_id"])

        assert state["armed"] is False
        assert await env.page(session_id).evaluate("window.clicks") == 1


# ── A page element named like the registry ───────────────────────────────────


async def test_an_element_named_like_the_registry_does_not_stand_in_for_it(tmp_path: Path) -> None:
    """Codex 9: Window named access made a page element answer for globalThis.__autoBrowserElementIds."""
    page_html = """<title>named</title><div id="__autoBrowserElementIds"></div>
<button id="go" onclick="document.title='GO'">Go</button>"""
    async with browser_env(tmp_path, {"/": page_html}) as env:
        session_id = await env.create("/")
        go = await env.element(session_id, "Go")

        await env.manager.click(session_id, element_id=go["element_id"])

        assert await env.page(session_id).title() == "GO"
