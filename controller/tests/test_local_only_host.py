"""One definition of a host that names only this machine (NAV-29, AUTH-34)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.auth_policy import is_local_only_host
from app.browser.services.remote_access import BrowserRemoteAccessService
from app.config import Settings
from app.runtime_policy import validate_runtime_policy
from app.session_tunnel import IsolatedSessionTunnelBroker

LOCAL = [
    "",
    "localhost",
    "LOCALHOST",
    "app.localhost",
    "127.0.0.1",
    "127.0.0.2",
    "::1",
    "::ffff:127.0.0.1",
    "0.0.0.0",
    "::",
]
NOT_LOCAL = [
    "example.com",
    "10.0.0.5",
    "192.168.1.9",
    "notlocalhost",
    "localhost.example.com",
    "8.8.8.8",
    "::ffff:8.8.8.8",
]
WARNING = "TAKEOVER_URL is still local-only"


class IsLocalOnlyHostTests(unittest.TestCase):
    def test_local_names_and_addresses(self) -> None:
        for host in LOCAL:
            with self.subTest(host=host):
                self.assertTrue(is_local_only_host(host))

    def test_everything_else_is_not(self) -> None:
        for host in NOT_LOCAL:
            with self.subTest(host=host):
                self.assertFalse(is_local_only_host(host))


class CallersAgreeTests(unittest.TestCase):
    def test_remote_access_reads_a_takeover_url_by_it(self) -> None:
        for url in ("http://127.0.0.2:6080/vnc.html", "http://[::ffff:127.0.0.1]:6080/", "http://app.localhost/"):
            with self.subTest(url=url):
                self.assertTrue(BrowserRemoteAccessService.takeover_url_is_local_only(url))
        self.assertFalse(BrowserRemoteAccessService.takeover_url_is_local_only("https://takeover.example.com/"))

    def test_the_runtime_policy_warns_for_each_local_takeover_url(self) -> None:
        for url in ("http://127.0.0.2:6080/vnc.html", "http://app.localhost:6080/", "http://[::ffff:127.0.0.1]:6080/"):
            with self.subTest(url=url):
                report = validate_runtime_policy(Settings(_env_file=None, APP_ENV="production", TAKEOVER_URL=url))
                self.assertTrue(any(WARNING in warning for warning in report.warnings), report.warnings)

    def test_the_runtime_policy_does_not_warn_for_a_public_takeover_url(self) -> None:
        report = validate_runtime_policy(
            Settings(_env_file=None, APP_ENV="production", TAKEOVER_URL="https://takeover.example.com/vnc.html")
        )
        self.assertFalse(any(WARNING in warning for warning in report.warnings))


class TunnelBindAddressTests(unittest.TestCase):
    def broker(self, bind_address: str, root: Path) -> IsolatedSessionTunnelBroker:
        (root / "id").write_text("k", encoding="utf-8")
        (root / "known_hosts").write_text("k", encoding="utf-8")
        return IsolatedSessionTunnelBroker(
            Settings(
                _env_file=None,
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(root / "id"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(root / "known_hosts"),
                ISOLATED_TUNNEL_INFO_ROOT=str(root / "tunnels"),
                ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS=bind_address,
            )
        )

    def test_another_loopback_address_is_a_local_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.broker("127.0.0.2", Path(tmp))._validate_settings()

    def test_all_interfaces_is_still_not_a_local_bind(self) -> None:
        # 0.0.0.0 is a "local-only" takeover host (nothing is reachable there) but a PUBLIC remote bind.
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            self.broker("0.0.0.0", Path(tmp))._validate_settings()


if __name__ == "__main__":
    unittest.main()
