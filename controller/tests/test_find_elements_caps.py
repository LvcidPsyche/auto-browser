"""find_elements and the string caps: regex matches, secrets, marks, surrogates and a hostile page (W4-7, W4-19, W4-20,
W4-37, W4-35, W4-6, W4-11, W4-36).

The scripts run under Node against minimal fake DOM elements (as in test_observation_string_caps.py); the Python side
runs against fake pages.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import browser_scripts
from app.browser_scripts import (
    AB_ATTR_MAX,
    AB_HREF_MAX,
    AB_MATCH_MAX,
    AB_TRUNCATED_MARK,
    cap_found_elements,
    cap_words,
)
from app.tool_inputs import FindElementsInput
from tests.test_allowed_hosts_reads import gateway_for, manager_for, read_session

NODE = shutil.which("node")
HUGE = 1_000_000
EMOJI = "\U0001f600"

# `%(...)s` slots take script sources; `%(case)s` is a JSON object describing the fake element and text node.
_RUNNER = r"""
const CASE = %(case)s;
function fake(attrs, extra) {
  return Object.assign({
    tagName: 'INPUT', innerText: 'label', value: '', id: '', className: '',
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    getBoundingClientRect: () => ({ x: 1, y: 2, width: 30, height: 10 }),
  }, extra);
}
const el = fake(CASE.attrs, { value: CASE.value, id: CASE.id, className: CASE.className, href: CASE.href });
globalThis.NodeFilter = { SHOW_TEXT: 4 };
let walked = false;
el.parentElement = el;
globalThis.document = {
  body: {},
  querySelectorAll: () => [el],
  createTreeWalker: () => ({
    nextNode: () => (walked ? null : ((walked = true), { textContent: CASE.text, parentElement: el })),
  }),
};
if (CASE.hostile) { String.prototype.slice = function () { return String(this); }; }
const byText = (%(find_text)s)([CASE.query, CASE.regex, CASE.context, 5]);
walked = false;
const bySelector = (%(find_selector)s)(['input', 5]);
console.log(JSON.stringify({ byText, bySelector }));
"""


def _run(**case: object) -> dict:
    base = {
        "attrs": {},
        "value": "",
        "id": "",
        "className": "",
        "href": "",
        "text": "needle",
        "query": "needle",
        "regex": False,
        "context": 5,
        "hostile": False,
    }
    source = _RUNNER % {
        "case": json.dumps({**base, **case}),
        "find_text": browser_scripts.FIND_ELEMENTS_TEXT_SCRIPT,
        "find_selector": browser_scripts.FIND_ELEMENTS_SELECTOR_SCRIPT,
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


needs_node = pytest.mark.skipif(NODE is None, reason="node not available")


@needs_node
def test_a_greedy_regex_match_and_its_context_are_capped() -> None:
    context = 10
    out = _run(text="x" * HUGE, query=r"[\s\S]+", regex=True, context=context)

    found = out["byText"][0]
    assert len(found["match"]) == AB_MATCH_MAX
    assert len(found["context_text"]) <= AB_MATCH_MAX + 2 * context
    assert len(found["text"]) <= browser_scripts.AB_TEXT_MAX


@needs_node
def test_the_context_around_a_short_match_is_bounded_by_the_request_on_each_side() -> None:
    out = _run(text="a" * 100 + "needle" + "b" * 100, context=7)

    assert out["byText"][0]["context_text"] == "a" * 7 + "needle" + "b" * 7


@needs_node
def test_a_cut_value_id_or_class_says_so_and_a_whole_one_does_not() -> None:
    out = _run(value="v" * 500, id="i" * 500, className="c" * 500)

    for found in (out["byText"][0], out["bySelector"][0]):
        for key in ("value", "id", "class"):
            assert len(found[key]) == AB_ATTR_MAX
            assert found[f"{key}_truncated"] is True

    short = _run(value="hello", id="main", className="btn primary")
    for found in (short["byText"][0], short["bySelector"][0]):
        assert (found["value"], found["id"], found["class"]) == ("hello", "main", "btn primary")
        assert not [key for key in found if key.endswith("_truncated")]


@needs_node
def test_a_long_class_list_is_cut_between_classes() -> None:
    classes = " ".join(f"tw-class-{index}" for index in range(100))
    out = _run(className=classes)

    for found in (out["byText"][0], out["bySelector"][0]):
        assert found["class_truncated"] is True
        assert classes.startswith(found["class"])
        assert len(found["class"]) <= AB_ATTR_MAX
        assert classes[len(found["class"])] == " "
        assert found["class"].split(" ")[-1].startswith("tw-class-")
        assert found["class"].split(" ")[-1] in classes.split(" ")


@needs_node
def test_the_script_sends_the_attributes_the_secret_check_reads_only_for_a_field_with_a_value() -> None:
    out = _run(value="hunter2", attrs={"type": "password", "name": "pw"})

    assert out["bySelector"][0]["field"]["type"] == "password"
    assert "field" not in _run(value="")["bySelector"][0]


@needs_node
def test_a_cut_never_leaves_half_of_a_surrogate_pair() -> None:
    # One unit short of the cap, so the emoji's high surrogate would be the last kept unit.
    value = "x" * (AB_ATTR_MAX - 1) + EMOJI + "tail"
    href = "https://example.com/" + "h" * (AB_HREF_MAX - len(AB_TRUNCATED_MARK) - 21) + EMOJI + "t" * 50
    out = _run(value=value, id=value, className=value, href=href)

    for found in (out["byText"][0], out["bySelector"][0]):
        for key in ("value", "id", "class", "href"):
            assert not _has_lone_surrogate(found[key]), key
            found[key].encode("utf-8")
        assert found["value"] == "x" * (AB_ATTR_MAX - 1)
        assert found["href"].endswith(AB_TRUNCATED_MARK)
        assert len(found["href"]) <= AB_HREF_MAX


@needs_node
def test_the_context_never_starts_or_ends_inside_an_emoji() -> None:
    # Context 1 around "abc": the window's start lands on the emoji's low surrogate on the left, and its end on the
    # emoji's high surrogate on the right.
    left = _run(text=EMOJI + "abc", query="abc", context=1)["byText"][0]["context_text"]
    right = _run(text="abc" + EMOJI, query="abc", context=1)["byText"][0]["context_text"]
    both = _run(text=EMOJI + "abc" + EMOJI, query="abc", context=1)["byText"][0]["context_text"]

    assert (left, right, both) == ("abc", "abc", "abc")
    # A window that holds the whole emoji keeps it.
    whole = _run(text=EMOJI + "abc" + EMOJI, query="abc", context=2)["byText"][0]["context_text"]
    assert whole == EMOJI + "abc" + EMOJI


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


@needs_node
def test_a_page_that_replaces_slice_gets_its_strings_through_the_script_but_not_past_the_python_recap() -> None:
    out = _run(
        value="v" * HUGE, id="i" * HUGE, className="c" * HUGE, href="https://e.example/" + "h" * HUGE, hostile=True
    )
    found = out["bySelector"][0]
    assert len(found["value"]) == HUGE  # the page defeated the in-page cap, as the finding says

    (shown,) = cap_found_elements([found], 0, lambda _field: False)

    assert len(shown["value"]) == AB_ATTR_MAX
    assert len(shown["id"]) == AB_ATTR_MAX
    assert len(shown["class"]) <= AB_ATTR_MAX
    assert len(shown["href"]) == AB_HREF_MAX
    assert shown["href"].endswith(AB_TRUNCATED_MARK)
    assert shown["value_truncated"] is True
    assert shown["id_truncated"] is True
    assert shown["class_truncated"] is True


def test_cap_words_cuts_between_words() -> None:
    assert cap_words("aaa bbb ccc", 8) == "aaa bbb"
    assert cap_words("aaa bbb ccc", 7) == "aaa bbb"
    assert cap_words("a" * 20, 8) == "a" * 8
    assert cap_words("short", 8) == "short"


# ── the gateway: Python recap and secret fields ──


def _gateway_for(tmp_path: Path, evaluated: object):
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path, evaluated=evaluated)
    session.page.move = None
    return gateway_for(manager)


def _element(**extra: object) -> dict:
    return {"tag": "input", "text": "", "value": None, "href": None, "id": None, "class": None, **extra}


async def test_a_password_fields_value_is_withheld_and_a_plain_fields_is_not(tmp_path: Path) -> None:
    elements = [
        _element(value="hunter2", field={"type": "password", "name": "pw"}),
        _element(value="123456", field={"type": "text", "name": "otp-code"}),
        _element(value="jane@example.com", field={"type": "email", "name": "email"}),
        _element(value=None),
    ]
    gateway = _gateway_for(tmp_path, elements)

    selector = await gateway._find_elements(FindElementsInput(session_id="s1", selector="input"))
    query = await gateway._find_elements(FindElementsInput(session_id="s1", query="x"))

    for result in (selector, query):
        secret, otp, plain, empty = result["elements"]
        assert secret["value"] is None
        assert secret["value_redacted"] is True
        assert otp["value"] is None
        assert otp["value_redacted"] is True
        assert plain["value"] == "jane@example.com"
        assert "value_redacted" not in plain
        assert empty["value"] is None
        assert "value_redacted" not in empty
        assert not any("field" in element for element in result["elements"])
    assert "hunter2" not in json.dumps([selector, query])


async def test_a_hostile_pages_oversized_strings_are_cut_again_in_python(tmp_path: Path) -> None:
    huge = "z" * HUGE
    elements = [
        _element(
            text=huge,
            match=huge,
            context_text=huge,
            value=huge,
            id=huge,
            **{"class": huge},
            href="https://e.example/" + huge,
        )
    ]
    gateway = _gateway_for(tmp_path, elements)

    result = await gateway._find_elements(FindElementsInput(session_id="s1", query="x", regex=True, context=10))

    (found,) = result["elements"]
    assert len(found["text"]) == browser_scripts.AB_TEXT_MAX
    assert len(found["match"]) == AB_MATCH_MAX
    assert len(found["context_text"]) == AB_MATCH_MAX + 2 * 10
    assert len(found["href"]) == AB_HREF_MAX
    assert found["value_truncated"] is True
    assert len(json.dumps(result)) < 20_000


async def test_an_ordinary_result_passes_through_unchanged(tmp_path: Path) -> None:
    elements = [_element(text="Hello", value="v", id="main", href="https://example.com/a?b=c", **{"class": "btn"})]
    gateway = _gateway_for(tmp_path, [dict(element) for element in elements])

    result = await gateway._find_elements(FindElementsInput(session_id="s1", selector="input"))

    assert result["elements"] == elements


def test_the_recap_tolerates_a_result_that_is_not_a_list_of_dicts() -> None:
    assert cap_found_elements("x" * HUGE, 0, lambda _f: False) == browser_scripts.cap_url("x" * HUGE)
    assert cap_found_elements([1, None, "s"], 0, lambda _f: False) == [1, None, "s"]
