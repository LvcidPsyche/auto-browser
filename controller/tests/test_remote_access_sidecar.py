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

    def test_an_oversized_stale_after_seconds_falls_back_too(self) -> None:
        # R-20: float(10**400) raises OverflowError, which was not caught, so the report raised.
        info = self.info(
            '{"status": "active", "updated_at": "2999-01-01T00:00:00Z", "stale_after_seconds": 1' + "0" * 400 + "}"
        )

        self.assertEqual(info["stale_after_seconds"], 45.0)
        self.assertEqual(info["status"], "active")

    def test_an_out_of_range_updated_at_falls_back_to_the_file_time(self) -> None:
        # fromisoformat reads it, and converting it to UTC overflows below year 1.
        info = self.info({"status": "active", "updated_at": "0001-01-01T00:00:00+01:00"})

        self.assertIsNotNone(info["last_updated"])

    def test_the_tunnel_block_carries_the_validated_values_not_the_raw_ones(self) -> None:
        # B-fix-5 (NAV-40): the block copied the file's raw values: NaN (invalid JSON for a strict client), "soon",
        # and a URL that was a list or an object.
        for content in (
            '{"status": "active", "updated_at": "2999-01-01T00:00:00Z", "stale_after_seconds": NaN}',
            {"status": "active", "updated_at": UPDATED, "stale_after_seconds": "soon"},
        ):
            with self.subTest(content=content):
                info = self.info(content)

                self.assertEqual(info["tunnel"]["stale_after_seconds"], 45.0)
        info = self.info(
            {"status": "active", "updated_at": UPDATED, "public_takeover_url": ["x"], "public_api_url": ""}
        )

        self.assertNotIn("public_takeover_url", info["tunnel"])
        self.assertNotIn("public_api_url", info["tunnel"])

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
