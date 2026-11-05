"""AUTO_BROWSER_REQUIRE_CHROMIUM turns "no Chromium" from a skip into a failure.

`requires_chromium` only skips, so a CI leg that installs Chromium could lose it (a failed
install, a moved browser root) and stay green while 20 real-browser test files skipped. The
leg that installs Chromium sets the variable, and a missing browser then fails collection.

These tests fake the browser absent; they never look for, or start, a real one.
"""

from __future__ import annotations

import pytest

from tests._chromium import ChromiumRequiredError, chromium_required, requires_chromium_mark


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_the_switch_is_on_for_truthy_values(value: str) -> None:
    assert chromium_required({"AUTO_BROWSER_REQUIRE_CHROMIUM": value}) is True


@pytest.mark.parametrize("environ", [{}, {"AUTO_BROWSER_REQUIRE_CHROMIUM": ""}, {"AUTO_BROWSER_REQUIRE_CHROMIUM": "0"}])
def test_the_switch_is_off_when_unset_empty_or_zero(environ: dict[str, str]) -> None:
    assert chromium_required(environ) is False


def test_without_the_switch_a_missing_chromium_skips() -> None:
    mark = requires_chromium_mark(None, required=False)

    assert mark.name == "skipif"
    assert mark.args == (True,)
    assert "no local Chromium" in mark.kwargs["reason"]


def test_with_the_switch_a_missing_chromium_fails() -> None:
    with pytest.raises(ChromiumRequiredError, match="AUTO_BROWSER_REQUIRE_CHROMIUM"):
        requires_chromium_mark(None, required=True)


@pytest.mark.parametrize("required", [False, True])
def test_a_found_chromium_never_skips_or_fails(required: bool) -> None:
    mark = requires_chromium_mark("/some/chrome", required=required)

    assert mark.name == "skipif"
    assert mark.args == (False,)
