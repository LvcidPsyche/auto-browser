"""A1-1: Codex makes each call's directory in CLI_HOME (OpenAIAdapter._call_directory). Under
docker-compose.host-subscriptions.yml before 1.18.1 that home was a directory the container runtime made, root-owned
0755, so every call failed while readiness said ready. Readiness and the production start now ask whether a directory
can be made there, and the overlay mounts a tmpfs at CLI_HOME."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.openai_adapter import OpenAIAdapter
from app.runtime_policy import validate_runtime_policy
from tests.test_runtime_policy import PRODUCTION_BEARER_TOKEN

OVERLAY = Path(__file__).resolve().parents[2] / "docker-compose.host-subscriptions.yml"
REFUSED = PermissionError(13, "Permission denied", "/never/shown")
_REAL_MKDTEMP = tempfile.mkdtemp


def _codex(cli_home: str) -> OpenAIAdapter:
    return OpenAIAdapter(Settings(_env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=cli_home))


def _production(cli_home: str) -> Settings:
    return Settings(
        _env_file=None, APP_ENV="production", API_BEARER_TOKEN=PRODUCTION_BEARER_TOKEN,
        SHARE_TOKEN_SECRET="share-secret-for-tests", REQUIRE_OPERATOR_ID="true",
        AUTH_STATE_ENCRYPTION_KEY="b" * 44, REQUIRE_AUTH_STATE_ENCRYPTION="true",
        ALLOWED_HOSTS="example.com", CONTROLLER_ALLOWED_HOSTS="controller.example.com",
        OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex", CLI_HOME=cli_home,
    )  # fmt: skip


def _mkdtemp_then(change):
    """tempfile.mkdtemp as a CLI running beside the check makes it look: the directory is made, then ``change``d before
    the check removes it."""

    def mkdtemp(*args, **kwargs):
        made = _REAL_MKDTEMP(*args, **kwargs)
        change(made)
        return made

    return mkdtemp


def _fill(directory: str) -> None:
    Path(directory, "planted").write_text("x", encoding="utf-8")


class CodexReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        Path(self.tempdir.name, ".codex").mkdir()
        Path(self.tempdir.name, ".claude").mkdir()

    def test_codex_is_not_ready_where_its_call_directory_cannot_be_made(self) -> None:
        adapter = _codex(self.tempdir.name)
        with (
            patch("app.providers.base.which", return_value="/usr/bin/codex"),
            patch("tempfile.mkdtemp", side_effect=REFUSED),
        ):
            self.assertFalse(adapter.configured)
            detail = adapter.readiness_detail
        self.assertIn("call directory", detail)
        self.assertIn(self.tempdir.name, detail)
        self.assertIn("Permission denied", detail)
        self.assertNotIn("/never/shown", detail)

    def test_codex_readiness_leaves_nothing_in_the_cli_home(self) -> None:
        adapter = _codex(self.tempdir.name)
        before = sorted(os.listdir(self.tempdir.name))
        with patch("app.providers.base.which", return_value="/usr/bin/codex"):
            self.assertTrue(adapter.configured, adapter.readiness_detail)
        self.assertEqual(sorted(os.listdir(self.tempdir.name)), before)

    def test_a_check_directory_a_cli_fills_meanwhile_leaves_codex_ready(self) -> None:
        # Its removal then fails (ENOTEMPTY): the directory was made, so the home can be written. One warning, which
        # names no path.
        adapter = _codex(self.tempdir.name)
        with (
            patch("app.providers.base.which", return_value="/usr/bin/codex"),
            patch("tempfile.mkdtemp", side_effect=_mkdtemp_then(_fill)),
            self.assertLogs("app.providers.base", "WARNING") as logs,
        ):
            self.assertTrue(adapter.configured, adapter.readiness_detail)
        self.assertNotIn(self.tempdir.name, "\n".join(logs.output))

    def test_a_check_directory_a_cli_removes_meanwhile_leaves_codex_ready(self) -> None:
        # Its removal then fails (FileNotFoundError).
        adapter = _codex(self.tempdir.name)
        with (
            patch("app.providers.base.which", return_value="/usr/bin/codex"),
            patch("tempfile.mkdtemp", side_effect=_mkdtemp_then(os.rmdir)),
        ):
            self.assertTrue(adapter.configured, adapter.readiness_detail)

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="POSIX, not root (root writes 0555)")
    def test_codex_is_not_ready_in_a_cli_home_this_user_cannot_write(self) -> None:
        os.chmod(self.tempdir.name, 0o555)  # noqa: S103 - read-only on purpose
        self.addCleanup(os.chmod, self.tempdir.name, 0o755)
        with patch("app.providers.base.which", return_value="/usr/bin/codex"):
            self.assertFalse(_codex(self.tempdir.name).configured)

    def test_claude_needs_no_call_directory(self) -> None:
        adapter = ClaudeAdapter(Settings(_env_file=None, CLAUDE_AUTH_MODE="cli", CLI_HOME=self.tempdir.name))
        with (
            patch("app.providers.base.which", return_value="/usr/bin/claude"),
            patch("tempfile.mkdtemp", side_effect=REFUSED),
        ):
            self.assertTrue(adapter.configured, adapter.readiness_detail)


class ProductionStartTests(unittest.TestCase):
    def test_production_refuses_codex_cli_mode_where_its_call_directory_cannot_be_made(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            Path(tempdir, ".codex").mkdir()
            with (
                patch("app.runtime_policy.which", return_value="/usr/bin/codex"),
                patch("tempfile.mkdtemp", side_effect=REFUSED),
            ):
                report = validate_runtime_policy(_production(tempdir))
        self.assertFalse(report.ok)
        self.assertTrue(
            any("call directory" in error and "Permission denied" in error for error in report.errors), report.errors
        )
        self.assertNotIn("/never/shown", " ".join(report.errors))

    def test_production_starts_where_a_cli_fills_the_check_directory_meanwhile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            Path(tempdir, ".codex").mkdir()
            with (
                patch("app.runtime_policy.which", return_value="/usr/bin/codex"),
                patch("tempfile.mkdtemp", side_effect=_mkdtemp_then(_fill)),
            ):
                report = validate_runtime_policy(_production(tempdir))
        self.assertTrue(report.ok, report.errors)


@pytest.mark.skipif(not OVERLAY.exists(), reason="the compose files are not shipped inside the controller image")
def test_the_overlay_mounts_a_tmpfs_at_the_cli_home_it_sets() -> None:
    text = OVERLAY.read_text(encoding="utf-8")
    cli_home = re.search(r"^\s+CLI_HOME:\s*(\S+)\s*$", text, re.M)
    assert cli_home, "the overlay sets CLI_HOME"
    tmpfs = re.search(r"-\s+type:\s*tmpfs\s*\n\s+target:\s*(\S+)", text)
    assert tmpfs and tmpfs.group(1) == cli_home.group(1), "a tmpfs is mounted at exactly CLI_HOME"
    # The host's sign-ins stay read-only inside it.
    assert text.count("read_only: true") == 4
