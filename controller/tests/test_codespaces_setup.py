"""setup.sh adds the forwarded controller host to a CONTROLLER_ALLOWED_HOSTS that .env already sets.

The overlay's wildcard is only a default: any CONTROLLER_ALLOWED_HOSTS in .env (the README's `cp .env.example .env`
puts a loopback-only one there) replaces it, and the forwarded name is then refused with "Invalid host header".
These tests run a scratch copy of .devcontainer/setup.sh with its installers stubbed out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[2] / ".devcontainer" / "setup.sh"
BASH = shutil.which("bash")

# .devcontainer/ is not shipped in the controller image.
pytestmark = [
    pytest.mark.skipif(not SETUP.is_file(), reason=".devcontainer/ is not shipped in the controller image"),
    pytest.mark.skipif(BASH is None, reason="needs bash"),
]

CODESPACE = "fluffy-space-123"
LOOPBACK = "localhost,127.0.0.1,::1"
TOKEN_LINE = "API_BEARER_TOKEN=already-set"


def _run(tmp_path: Path, env_text: str | None, *, codespace: str | None = CODESPACE, domain: str | None = None):
    work = tmp_path / "work"
    stubs = tmp_path / "stubs"
    work.mkdir(exist_ok=True)
    stubs.mkdir(exist_ok=True)
    (work / "controller").mkdir(exist_ok=True)
    # Installers and docker are stubbed: only the .env handling is under test.
    for name in ("pip", "docker", "python3"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    script = work / "setup.sh"
    script.write_bytes(SETUP.read_bytes().replace(b"\r\n", b"\n"))
    if env_text is not None:
        (work / ".env").write_text(env_text, encoding="utf-8", newline="\n")
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    if codespace is not None:
        env["CODESPACE_NAME"] = codespace
    if domain is not None:
        env["GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"] = domain
    result = subprocess.run(  # noqa: S603 - fixed argv, scratch copy
        [BASH, "setup.sh"], cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert result.returncode == 0, result.stderr[-2000:]
    dotenv = work / ".env"
    return (dotenv.read_text(encoding="utf-8") if dotenv.exists() else None), result.stdout + result.stderr


def _line(text: str, name: str) -> str:
    [line] = [line for line in text.splitlines() if line.startswith(f"{name}=")]
    return line


def test_the_forwarded_host_is_appended_to_an_existing_list(tmp_path: Path) -> None:
    dotenv, _ = _run(tmp_path, f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={LOOPBACK}\nOTHER=1\n", domain="app.github.dev")

    assert dotenv is not None
    assert (
        _line(dotenv, "CONTROLLER_ALLOWED_HOSTS")
        == f"CONTROLLER_ALLOWED_HOSTS={LOOPBACK},{CODESPACE}-8000.app.github.dev"
    )
    # The rest of the file is as it was.
    assert _line(dotenv, "API_BEARER_TOKEN") == TOKEN_LINE
    assert _line(dotenv, "OTHER") == "OTHER=1"


def test_the_forwarding_domain_defaults_to_app_github_dev(tmp_path: Path) -> None:
    dotenv, _ = _run(tmp_path, f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={LOOPBACK}\n", domain=None)

    assert dotenv is not None
    assert _line(dotenv, "CONTROLLER_ALLOWED_HOSTS").endswith(f",{CODESPACE}-8000.app.github.dev")


def test_a_custom_forwarding_domain_is_used(tmp_path: Path) -> None:
    dotenv, _ = _run(tmp_path, f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={LOOPBACK}\n", domain="preview.example")

    assert dotenv is not None
    assert _line(dotenv, "CONTROLLER_ALLOWED_HOSTS").endswith(f",{CODESPACE}-8000.preview.example")


def test_running_it_twice_adds_the_host_once(tmp_path: Path) -> None:
    first, _ = _run(tmp_path, f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={LOOPBACK}\n")
    second, _ = _run(tmp_path, None)

    assert first == second
    assert second is not None and second.count(f"{CODESPACE}-8000") == 1


def test_a_list_that_already_names_the_host_is_left_alone(tmp_path: Path) -> None:
    text = f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={CODESPACE}-8000.app.github.dev,{LOOPBACK}\n"

    dotenv, _ = _run(tmp_path, text)

    assert dotenv == text


def test_a_name_that_only_contains_the_host_does_not_count_as_naming_it(tmp_path: Path) -> None:
    longer = f"x{CODESPACE}-8000.app.github.dev"
    dotenv, _ = _run(tmp_path, f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={longer}\n")

    assert dotenv is not None
    assert (
        _line(dotenv, "CONTROLLER_ALLOWED_HOSTS")
        == f"CONTROLLER_ALLOWED_HOSTS={longer},{CODESPACE}-8000.app.github.dev"
    )


def test_no_list_in_env_leaves_the_overlay_default_in_charge(tmp_path: Path) -> None:
    dotenv, _ = _run(tmp_path, f"{TOKEN_LINE}\n")

    assert dotenv is not None
    assert "CONTROLLER_ALLOWED_HOSTS" not in dotenv


def test_an_empty_list_is_left_alone(tmp_path: Path) -> None:
    # Compose reads an empty value as unset and uses the overlay default.
    text = f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS=\n"

    dotenv, _ = _run(tmp_path, text)

    assert dotenv == text


def test_outside_a_codespace_env_is_not_touched(tmp_path: Path) -> None:
    text = f"CONTROLLER_ALLOWED_HOSTS={LOOPBACK}\n"

    dotenv, _ = _run(tmp_path, text, codespace=None)

    assert dotenv == text


def test_the_setting_is_not_printed(tmp_path: Path) -> None:
    _, output = _run(tmp_path, f"{TOKEN_LINE}\nCONTROLLER_ALLOWED_HOSTS={LOOPBACK}\n")

    assert LOOPBACK not in output
    assert TOKEN_LINE not in output
