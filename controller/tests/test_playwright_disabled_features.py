"""Storage partitioning stays on, and the rest of Playwright's --disable-features list stays as Playwright has it.

Chromium keeps the last --disable-features switch, so the controller (app/browser/launch_flags.py) and browser-node
(browser-node/server.mjs) pass Playwright's own list without ThirdPartyStoragePartitioning (decision 9). A
Playwright upgrade that changes its list would otherwise leave one copy re-enabling something Playwright now
disables, or dropping something it added. Both copies are compared with the list in the installed driver.

The browser-node half and the managed-policy half read the repo's browser-node/ directory, absent from the
controller image, so they skip there.

These compare the lists, not where the switch lands on the command line. That the appended --disable-features
comes after Playwright's own, and so wins, is proven on Chromium: with partitioning re-enabled a third-party
iframe's localStorage is keyed to the top-level site
(tests/test_storage_partitioning_chromium.py::test_what_partitioning_changes_for_a_third_party_iframes_local_storage),
and a cross-site iframe's worker does not serve a popup's first navigation
(tests/test_request_gate_hardening_chromium.py::test_a_cross_site_iframes_worker_does_not_serve_a_popups_first_navigation).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import playwright
import pytest

from app.browser.launch_flags import PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING, STORAGE_PARTITIONING_ON

PARTITIONING = "ThirdPartyStoragePartitioning"
CORE_BUNDLE = Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
BROWSER_NODE = Path(__file__).resolve().parents[2] / "browser-node"
_LIST = re.compile(r"disabledFeatures = \[(.*?)\]\.filter\(Boolean\)", re.S)
_JS_LIST = re.compile(r"const PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING = \[(.*?)\];", re.S)
_STRING = re.compile(r'"([^"]*)"')


def strings_only(array_source: str, where: str) -> list[str]:
    """The string entries of a JS array literal; anything else (a conditional entry) fails, naming where."""
    without_comments = re.sub(r"//[^\n]*", "", array_source)
    leftover = _STRING.sub("", without_comments).replace(",", "").strip()
    assert not leftover, f"{where}: the list has entries that are not plain strings, read it by hand: {leftover!r}"
    return _STRING.findall(without_comments)


def playwright_disabled_features() -> list[str]:
    match = _LIST.search(CORE_BUNDLE.read_text(encoding="utf-8"))
    assert match, f"{CORE_BUNDLE}: no `disabledFeatures = [...]` found; Playwright moved it"
    return strings_only(match.group(1), str(CORE_BUNDLE))


def test_playwright_still_disables_partitioning() -> None:
    """If it stops, the re-enable is moot: drop it rather than keep a copy of the list."""
    assert PARTITIONING in playwright_disabled_features()


def test_the_controllers_list_is_playwrights_without_partitioning() -> None:
    expected = [feature for feature in playwright_disabled_features() if feature != PARTITIONING]

    assert list(PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING) == expected
    assert STORAGE_PARTITIONING_ON == "--disable-features=" + ",".join(expected)


@pytest.mark.skipif(not BROWSER_NODE.is_dir(), reason="browser-node/ is not shipped inside the controller image")
def test_browser_nodes_list_is_playwrights_without_partitioning() -> None:
    source = (BROWSER_NODE / "server.mjs").read_text(encoding="utf-8")
    match = _JS_LIST.search(source)
    assert match, "browser-node/server.mjs: PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING not found"
    expected = [feature for feature in playwright_disabled_features() if feature != PARTITIONING]

    assert strings_only(match.group(1), "server.mjs") == expected
    assert '`--disable-features=${PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING.join(",")}`' in source


@pytest.mark.skipif(not BROWSER_NODE.is_dir(), reason="browser-node/ is not shipped inside the controller image")
def test_the_browser_node_image_turns_prediction_off_by_policy_in_both_paths() -> None:
    policy = json.loads((BROWSER_NODE / "policies" / "auto-browser.json").read_text(encoding="utf-8"))
    dockerfile = (BROWSER_NODE / "Dockerfile").read_text(encoding="utf-8")

    assert policy == {"NetworkPredictionOptions": 2}
    for managed in ("/etc/chromium/policies/managed/", "/etc/opt/chrome_for_testing/policies/managed/"):
        assert f"COPY policies/auto-browser.json {managed}auto-browser.json" in dockerfile
