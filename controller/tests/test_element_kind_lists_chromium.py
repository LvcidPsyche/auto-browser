"""The in-page scripts' lists of the element kinds that have `labels`, `disabled` and `href` are the browser's.

The scripts read `el.labels`, `el.disabled` and `el.href` only on an element whose kind (its tag name, upper-cased) is
in LABELABLE_TAGS (app/browser/element_scripts.py, in DOM_JS), HAS_DISABLED or HAS_HREF (app/browser_scripts.py, in
INTERACTABLES_SCRIPT): on any other element, such as a form whose named controls answer for those names, a read would
be the page's. Each list is taken from the script text as it ships, and held to the driver's Chromium both ways.

- Each listed kind has the property, as an element of the HTML namespace or of the SVG one (parsed from
  `<svg><KIND></KIND></svg>`, where the HTML parser restores an SVG name's case; a kind the parser moves out of
  `<svg>` is judged by its HTML element alone).
- Each carrier interface, an interface on `window` whose prototype has the property and is an element's, is the
  interface of some listed kind's element in either namespace. An abstract one (SVGGradientElement) is covered by the
  kinds that inherit from it, and a named constructor (Option) by its element's kind.

The property is first shown to be on no prototype every element has (Element, HTMLElement, SVGElement): there the list
would mean nothing. The page is about:blank, and window's properties are read through their descriptors: touching an
accessor such as localStorage throws on a page without an origin.

The limit: a kind that is not listed but shares its interface with a listed kind is not noticed.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from app import browser_scripts
from app.browser import element_scripts
from tests._chromium import chromium_executable, requires_chromium

# (the property, the list's name, the script it ships in)
LISTS = [
    ("labels", "LABELABLE_TAGS", element_scripts.DOM_JS),
    ("disabled", "HAS_DISABLED", browser_scripts.INTERACTABLES_SCRIPT),
    ("href", "HAS_HREF", browser_scripts.INTERACTABLES_SCRIPT),
]
# Asked about beside the listed kinds, for the test of a kind added to a list: a form has none of the three of its own.
UNLISTED = "FORM"

PAGE_JS = """
([prop, kinds]) => {
  const SVG = 'http://www.w3.org/2000/svg';
  const generic = ['Element', 'HTMLElement', 'SVGElement'].filter((name) => prop in window[name].prototype);
  const interfaces = new Map();
  for (const name of Object.getOwnPropertyNames(window)) {
    const value = Object.getOwnPropertyDescriptor(window, name).value;
    if (typeof value === 'function' && Element.prototype.isPrototypeOf(value.prototype)) interfaces.set(name, value);
  }
  const carriers = [...interfaces].filter(([, I]) => prop in I.prototype).map(([name]) => name).sort();
  const answers = {};
  for (const kind of kinds) {
    const made = [document.createElement(kind.toLowerCase())];
    const box = document.createElement('div');
    box.innerHTML = '<svg><' + kind + '></' + kind + '></svg>';
    const child = box.firstElementChild && box.firstElementChild.firstElementChild;
    if (child && child.namespaceURI === SVG) made.push(child);
    answers[kind] = {
      made: made.map((el) => el.namespaceURI + ' ' + el.localName + ': ' + el.constructor.name),
      has: made.some((el) => prop in el),
      instances: carriers.filter((name) => made.some((el) => el instanceof interfaces.get(name))),
    };
  }
  return { generic, carriers, kinds: answers };
}
"""


def shipped(script: str, name: str) -> list[str]:
    """The kinds quoted in the script's one `const NAME = [...];`."""
    found = re.findall(rf"\bconst {name} = \[(.*?)\];", script, flags=re.S)
    assert len(found) == 1, f"{len(found)} definitions of {name}"
    kinds = re.findall(r"'([^']*)'", found[0])
    assert kinds and all(re.fullmatch(r"[A-Z]+", kind) for kind in kinds), kinds
    return kinds


def judge(prop: str, kinds: list[str], answer: dict) -> list[str]:
    """What is wrong with `kinds` as the kinds of element that have `prop`, by what the page answered."""
    problems = []
    for kind in kinds:
        if not answer["kinds"][kind]["has"]:
            made = answer["kinds"][kind]["made"]
            problems.append(
                f"{kind}: no element of this kind has `{prop}`, in the HTML namespace or the SVG one ({made})"
            )
    covered = {name for kind in kinds for name in answer["kinds"][kind]["instances"]}
    problems += [
        f"{name}: has `{prop}`, and no listed kind's element is one"
        for name in answer["carriers"]
        if name not in covered
    ]
    return problems


@pytest.fixture(scope="module")
def answers() -> dict[str, dict]:
    """What the driver's Chromium answers for each property, about its list's kinds and UNLISTED."""
    from playwright.async_api import async_playwright

    async def ask() -> dict[str, dict]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                page = await browser.new_page()
                return {
                    prop: await page.evaluate(PAGE_JS, [prop, [*shipped(script, name), UNLISTED]])
                    for prop, name, script in LISTS
                }
            finally:
                await browser.close()

    return asyncio.run(ask())


def _ids(cases: list[tuple]) -> list[str]:
    return [case[1] for case in cases]


@requires_chromium
@pytest.mark.parametrize(("prop", "name", "script"), LISTS, ids=_ids(LISTS))
def test_each_listed_kind_has_the_property_and_each_interface_with_it_is_listed(
    answers: dict[str, dict], prop: str, name: str, script: str
) -> None:
    answer = answers[prop]

    assert answer["generic"] == [], f"every element has `{prop}`: {answer['generic']}"
    assert answer["carriers"], f"no interface on window has `{prop}`: the enumeration is wrong"
    assert judge(prop, shipped(script, name), answer) == []


# A kind taken out of each list, and the interface no other listed kind's element is.
TAKEN_OUT = [
    ("labels", "LABELABLE_TAGS", element_scripts.DOM_JS, "METER", "HTMLMeterElement"),
    ("disabled", "HAS_DISABLED", browser_scripts.INTERACTABLES_SCRIPT, "FIELDSET", "HTMLFieldSetElement"),
    ("href", "HAS_HREF", browser_scripts.INTERACTABLES_SCRIPT, "USE", "SVGUseElement"),
]


@requires_chromium
@pytest.mark.parametrize(("prop", "name", "script", "kind", "interface"), TAKEN_OUT, ids=_ids(TAKEN_OUT))
def test_a_kind_taken_out_of_a_list_leaves_its_interface_named(
    answers: dict[str, dict], prop: str, name: str, script: str, kind: str, interface: str
) -> None:
    kinds = shipped(script, name)
    assert kind in kinds

    problems = judge(prop, [listed for listed in kinds if listed != kind], answers[prop])

    assert len(problems) == 1 and problems[0].startswith(f"{interface}:"), problems


@requires_chromium
@pytest.mark.parametrize(("prop", "name", "script"), LISTS, ids=_ids(LISTS))
def test_a_kind_added_to_a_list_without_the_property_is_named(
    answers: dict[str, dict], prop: str, name: str, script: str
) -> None:
    problems = judge(prop, [*shipped(script, name), UNLISTED], answers[prop])

    assert len(problems) == 1 and problems[0].startswith(f"{UNLISTED}:"), problems
