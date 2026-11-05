"""The remote-access sidecar file is read defensively and only part of it is passed on (NAV-40)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_auth_state_ownership import build_manager

UPDATED = "2999-01-01T00:00:00Z"


class SidecarTests(unittest.TestCase):
    def info(self, content) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = build_manager(Path(tmp))
            path = Path(manager.settings.remote_access_info_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
            return manager.remote_access.global_info()

    def test_only_the_whitelisted_fields_are_passed_on(self) -> None:
        info = self.info(
            {
                "status": "active",
                "updated_at": UPDATED,
                "public_takeover_url": "https://takeover.example.org/vnc.html",
                "public_api_url": "https://api.example.org",
                "stale_after_seconds": 90,
                "ssh_host": "bastion.example.com",
                "ssh_user": "tunnel",
                "pid": 4242,
                "log_path": "/srv/tunnels/reverse-ssh.log",
                "error": "Permission denied (publickey)",
            }
        )

        self.assertEqual(
            info["tunnel"],
            {
                "status": "active",
                "updated_at": UPDATED,
                "public_takeover_url": "https://takeover.example.org/vnc.html",
                "public_api_url": "https://api.example.org",
                "stale_after_seconds": 90,
            },
        )
        self.assertTrue(info["active"])
        self.assertEqual(info["takeover_url"], "https://takeover.example.org/vnc.html")

    def test_a_non_numeric_stale_after_seconds_falls_back_to_the_setting(self) -> None:
        info = self.info({"status": "active", "updated_at": UPDATED, "stale_after_seconds": "soon"})

        self.assertEqual(info["stale_after_seconds"], 45.0)
        self.assertEqual(info["status"], "active")

    def test_a_non_finite_stale_after_seconds_falls_back_too(self) -> None:
        info = self.info('{"status": "active", "updated_at": "2999-01-01T00:00:00Z", "stale_after_seconds": NaN}')

        self.assertEqual(info["stale_after_seconds"], 45.0)

    def test_a_file_that_is_not_an_object_is_unreadable_metadata(self) -> None:
        for content in ("[1, 2]", '"text"', "7", "null"):
            with self.subTest(content=content):
                info = self.info(content)

                self.assertEqual(info["status"], "error")
                self.assertEqual(info["error"], "remote_access_metadata_unreadable")
                self.assertIsNone(info["tunnel"])
                self.assertFalse(info["active"])

    def test_a_url_that_is_not_a_string_is_not_used(self) -> None:
        info = self.info(
            {"status": "active", "updated_at": UPDATED, "public_takeover_url": ["x"], "public_api_url": {"a": 1}}
        )

        self.assertEqual(info["takeover_url"], info["configured_takeover_url"])
        self.assertIsNone(info["api_url"])


if __name__ == "__main__":
    unittest.main()
