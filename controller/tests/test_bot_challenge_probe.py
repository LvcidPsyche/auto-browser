"""CHALLENGE_PROBE_SCRIPT's visibility computation, run for real (W4-24).

test_bot_challenge_service.py feeds the service `{"src", "visible"}` dicts, so the JavaScript that decides `visible`
never ran in any test: a probe that said "visible" for every frame would have passed them all. This runs the script
under Node against a fake DOM, as test_observation_string_caps.py does for the observation scripts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.browser.services.bot_challenge import CHALLENGE_PROBE_SCRIPT

NODE = shutil.which("node")

_RUNNER = r"""
const FRAMES = %(frames)s;
const frames = FRAMES.map((f) => ({
  src: f.src,
  getAttribute: (name) => (name === 'src' ? f.src : null),
  getBoundingClientRect: () => ({ width: f.width, height: f.height }),
  style: { display: f.display, visibility: f.visibility },
}));
globalThis.document = {
  title: 'Sign in',
  body: { innerText: 'welcome' },
  querySelectorAll: (sel) => (sel === 'iframe' ? frames : []),
};
globalThis.window = { getComputedStyle: (el) => el.style };
console.log(JSON.stringify((%(probe)s)()));
"""

_FRAME = {"width": 300, "height": 80, "display": "block", "visibility": "visible"}


def _probe(frames: list[dict]) -> dict:
    source = _RUNNER % {"frames": json.dumps(frames), "probe": CHALLENGE_PROBE_SCRIPT}
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


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_the_probe_marks_only_a_rendered_visible_frame_visible() -> None:
    frames = [
        {**_FRAME, "src": "https://a.example/zero", "width": 0, "height": 0},
        {**_FRAME, "src": "https://a.example/no-height", "height": 0},
        {**_FRAME, "src": "https://a.example/none", "display": "none"},
        {**_FRAME, "src": "https://a.example/hidden", "visibility": "hidden"},
        {**_FRAME, "src": "https://a.example/shown"},
    ]

    out = _probe(frames)

    assert [(item["src"], item["visible"]) for item in out["iframes"]] == [
        ("https://a.example/zero", False),
        ("https://a.example/no-height", False),
        ("https://a.example/none", False),
        ("https://a.example/hidden", False),
        ("https://a.example/shown", True),
    ]
    assert out["title"] == "Sign in"
    assert out["text"] == "welcome"
