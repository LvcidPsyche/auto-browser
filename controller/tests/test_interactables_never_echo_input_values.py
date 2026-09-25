"""An input's live value must never be reported as its label.

INTERACTABLES_SCRIPT and ACTIVE_ELEMENT_SCRIPT used to fall back to `el.value` when a field had
no aria-label / placeholder / innerText -- the normal case for a bare <input>. After a password
or one-time code was typed, every later observe() reported the secret as that element's
`label`, and it flowed to the calling agent's model and into actions.jsonl / audit / witness.
Only a button-like input (button/submit/reset), whose value IS its visible caption, may use it.
"""

from __future__ import annotations

import re

from app import browser_scripts


def _bare_value_uses(source: str) -> list[str]:
    # Every `el.value` that is not inside the explicit button-type guard.
    lines = [line for line in source.splitlines() if re.search(r"\bel\.value\b", line)]
    return [line for line in lines if "button" not in line or "submit" not in line]


def test_interactables_label_never_uses_a_field_value():
    assert _bare_value_uses(browser_scripts.INTERACTABLES_SCRIPT) == []


def test_active_element_label_never_uses_a_field_value():
    assert _bare_value_uses(browser_scripts.ACTIVE_ELEMENT_SCRIPT) == []
