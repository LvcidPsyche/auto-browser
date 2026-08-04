"""Every shipped JavaScript snippet must actually parse as JavaScript.

`STEALTH_INIT_SCRIPT` contained two shell-escape artifacts — `if (\\!window.chrome)`
and `typeof WebGL2RenderingContext \\!== 'undefined'` — inside an r-string, so the
backslashes reached the JS source verbatim. `page.add_init_script` installed a
script that threw `SyntaxError` at parse time in every page context, meaning no
stealth patch had *ever* applied. The failure happened page-side, so nothing in
Python saw an exception and nothing in CI went red.

These strings are only ever executed by a browser, so Python's own tooling can
never validate them. This gate parses each one with Node.

Skips cleanly when node is unavailable so the suite stays portable; CI has Node
24 on the runner.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import browser_scripts

NODE = shutil.which("node")

SCRIPT_NAMES = sorted(
    name
    for name in dir(browser_scripts)
    if name.endswith("_SCRIPT") and isinstance(getattr(browser_scripts, name), str)
)


def _node_check(source: str) -> tuple[bool, str]:
    """True if node can parse `source` as a script."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        handle.write(source)
    finally:
        handle.close()
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, temp path
            [NODE, "--check", handle.name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0, result.stderr
    finally:
        Path(handle.name).unlink(missing_ok=True)


def test_script_constants_were_discovered() -> None:
    """Guard the guard — an empty list would make every case below vacuous."""
    assert len(SCRIPT_NAMES) >= 10
    assert "STEALTH_INIT_SCRIPT" in SCRIPT_NAMES


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_script_is_valid_javascript(name: str) -> None:
    source = getattr(browser_scripts, name)

    # Statement-style scripts (STEALTH_INIT_SCRIPT) parse as-is. Expression-style
    # ones (the `(args) => {...}` snippets passed to page.evaluate) need wrapping
    # to be a complete script. Either shape is legitimate here.
    ok, stderr = _node_check(source)
    if not ok:
        ok, stderr = _node_check(f"void (\n{source}\n);")

    assert ok, f"{name} is not valid JavaScript:\n{stderr}"


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_no_shell_escape_artifacts_in_scripts() -> None:
    r"""`\!` is never valid JS and is the specific artifact that shipped."""
    for name in SCRIPT_NAMES:
        source = getattr(browser_scripts, name)
        assert "\\!" not in source, (
            f"{name} contains a backslash-bang, a shell-escaping artifact. "
            r"In JS `\!` is a syntax error, so the whole script fails to parse "
            "page-side and silently does nothing."
        )
