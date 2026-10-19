"""reverse-ssh/entrypoint.sh writes reverse-ssh.json for the controller to read.

The sidecar rewrites the file every few seconds and the controller reads it at any
moment, so a write must be atomic (a reader never sees half a file) and every
value must be JSON-escaped (a quote in a host name must not corrupt the file).
The sidecar image is Alpine without Python, so the writer is POSIX sh; these tests
pull its functions out of the entrypoint and run them under the host's sh.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "reverse-ssh" / "entrypoint.sh"
SH = shutil.which("sh")

# reverse-ssh/ is not shipped in the controller image.
pytestmark = [
    pytest.mark.skipif(not ENTRYPOINT.is_file(), reason="reverse-ssh/ is not shipped in the controller image"),
    pytest.mark.skipif(SH is None, reason="needs a POSIX sh"),
]

INFO_NAME = "reverse-ssh.json"
BASE_ENV = {
    "REVERSE_SSH_INFO_PATH": INFO_NAME,
    "REVERSE_SSH_INFO_INTERVAL_SECONDS": "15",
    "REVERSE_SSH_STALE_AFTER_SECONDS": "45",
    "REVERSE_SSH_HOST": "bastion.example.com",
    "REVERSE_SSH_PORT": "22",
    "REVERSE_SSH_USER": "tunnel",
    "REVERSE_SSH_ACCESS_MODE": "private",
    "REVERSE_SSH_REMOTE_BIND_ADDRESS": "127.0.0.1",
    "REVERSE_SSH_REMOTE_API_PORT": "18000",
    "REVERSE_SSH_REMOTE_NOVNC_PORT": "16080",
    "REVERSE_SSH_PUBLIC_API_URL": "http://bastion.example.com:18000",
    "REVERSE_SSH_PUBLIC_TAKEOVER_URL": "http://bastion.example.com:16080/vnc.html?autoconnect=true&resize=scale",
}


def shell_functions() -> str:
    """The writer's top-level functions, verbatim from the entrypoint."""
    source = ENTRYPOINT.read_text(encoding="utf-8")
    parts = []
    for name in ("now_iso8601", "json_str", "write_metadata"):
        # The heredoc inside write_metadata has its own "}" at column 0; the function's
        # closing brace is the one followed by a blank line.
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n(?=\n)", source, re.S | re.M)
        if match:
            parts.append(match.group(0))
    assert parts, f"no writer functions found in {ENTRYPOINT.name}"
    return "".join(parts)


def run_writer(tmp_path: Path, prelude: str = "", state: str = "active", **env: str) -> Path:
    result = subprocess.run(
        # The script goes in on stdin: Windows argv quoting would eat its backslashes.
        [SH, "-eu"],
        input=f'{prelude}\n{shell_functions()}\nwrite_metadata "{state}"\n',
        cwd=tmp_path,
        env={**os.environ, **BASE_ENV, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path / INFO_NAME


def test_values_are_json_escaped(tmp_path: Path) -> None:
    hostile = 'bast"ion\\x'
    info = json.loads(run_writer(tmp_path, REVERSE_SSH_HOST=hostile).read_text(encoding="utf-8"))
    assert info["ssh_host"] == hostile
    assert info["status"] == "active"
    assert info["ssh_port"] == 22
    assert info["remote_novnc_port"] == 16080
    assert info["public_takeover_url"].endswith("/vnc.html?autoconnect=true&resize=scale")


def test_the_file_is_replaced_by_a_rename_and_nothing_is_left_behind(tmp_path: Path) -> None:
    target = tmp_path / INFO_NAME
    target.write_text('{"status": "old"}', encoding="utf-8")
    # Record every mv, then run the real one.
    prelude = 'mv() { echo "$1 $2" >>moves.log; command mv "$@"; }'
    run_writer(tmp_path, prelude, state="starting")
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "starting"
    moves = (tmp_path / "moves.log").read_text(encoding="utf-8").splitlines()
    assert len(moves) == 1
    source, destination = moves[0].rsplit(" ", 1)
    assert destination == INFO_NAME
    assert source != INFO_NAME  # the content was written elsewhere first...
    assert os.path.dirname(source) == os.path.dirname(destination)  # ...on the same filesystem
    assert sorted(p.name for p in tmp_path.iterdir()) == ["moves.log", INFO_NAME]
