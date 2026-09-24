"""A mesh grant's url_allowlist must hold for every URL a peer can make us load.

Two ways around it existed. The check read only top-level `url`/`start_url`,
so browser.execute_action — whose target sits at `action.url` — was never
checked. And patterns were fnmatch'd over the whole URL, where `*` also matches
"/", "?", "#" and "@", so `https://*.example.com/*` admitted
`https://evil.com/.example.com/`.
"""

from __future__ import annotations

import unittest

from app.mesh.models import CapabilityGrant, DelegationRequest, PeerRecord
from app.mesh.policy import PolicyDenied, PolicyEvaluator


def _evaluate(patterns: list[str], arguments: dict, capability: str = "tool:browser.execute_action"):
    peer = PeerRecord(
        node_id="peer1",
        pubkey_b64="A" * 44,
        grants=[CapabilityGrant(capability="tool:*", url_allowlist=patterns)],
    )
    return PolicyEvaluator().evaluate(peer, DelegationRequest(capability=capability, arguments=arguments))


class NestedUrlArgumentTests(unittest.TestCase):
    def test_execute_action_navigation_target_is_checked(self) -> None:
        arguments = {"session_id": "s1", "action": {"action": "navigate", "url": "https://evil.com/"}}

        with self.assertRaises(PolicyDenied):
            _evaluate(["https://example.com/*"], arguments)

    def test_an_allowlisted_nested_target_is_permitted(self) -> None:
        arguments = {"session_id": "s1", "action": {"action": "navigate", "url": "https://example.com/next"}}

        self.assertIsNotNone(_evaluate(["https://example.com/*"], arguments))

    def test_cookie_urls_and_url_lists_are_checked(self) -> None:
        with self.assertRaises(PolicyDenied):
            _evaluate(["https://example.com/*"], {"cookies": [{"name": "a", "value": "b", "url": "https://evil.com/"}]})
        with self.assertRaises(PolicyDenied):
            _evaluate(["https://example.com/*"], {"urls": ["https://example.com/", "https://evil.com/"]})

    def test_requests_without_urls_are_unaffected(self) -> None:
        self.assertIsNotNone(_evaluate(["https://example.com/*"], {"session_id": "s1", "limit": 5}))


class PatternMatchingTests(unittest.TestCase):
    def _permits(self, pattern: str, url: str) -> bool:
        try:
            _evaluate([pattern], {"url": url}, capability="tool:browser.create_session")
        except PolicyDenied:
            return False
        return True

    def test_a_wildcard_host_cannot_reach_into_the_path(self) -> None:
        self.assertTrue(self._permits("https://*.example.com/*", "https://app.example.com/page"))
        self.assertFalse(self._permits("https://*.example.com/*", "https://evil.com/.example.com/"))
        self.assertFalse(self._permits("https://*.example.com/*", "https://evil.com/?q=.example.com/"))

    def test_scheme_less_patterns_match_the_host_only(self) -> None:
        self.assertTrue(self._permits("*example.com*", "https://example.com/page"))
        self.assertFalse(self._permits("*example.com*", "https://evil.com/?example.com"))
        self.assertTrue(self._permits("*.example.com", "http://api.example.com/v1"))
        self.assertFalse(self._permits("*.example.com", "http://example.com.evil.com/"))

    def test_userinfo_never_matches(self) -> None:
        self.assertFalse(self._permits("https://example.com/*", "https://example.com@evil.com/"))
        self.assertFalse(self._permits("*example.com*", "https://example.com:pw@evil.com/"))

    def test_scheme_port_and_case(self) -> None:
        self.assertFalse(self._permits("https://example.com/*", "http://example.com/"))
        self.assertFalse(self._permits("https://example.com/*", "https://example.com:8443/"))
        self.assertTrue(self._permits("https://example.com:*/*", "https://example.com:8443/"))
        self.assertTrue(self._permits("https://example.com/*", "HTTPS://Example.COM/x"))

    def test_ipv6_literals_keep_their_brackets(self) -> None:
        self.assertTrue(self._permits("http://[::1]:*/*", "http://[::1]:8000/x"))
        self.assertTrue(self._permits("http://[::1]/*", "http://[::1]/"))
        self.assertFalse(self._permits("http://[::1]/*", "http://[::2]/"))

    def test_a_pattern_without_a_path_matches_the_site_root_only(self) -> None:
        self.assertTrue(self._permits("https://example.com", "https://example.com"))
        self.assertTrue(self._permits("https://example.com", "https://example.com/"))
        self.assertFalse(self._permits("https://example.com", "https://example.com/admin"))


if __name__ == "__main__":
    unittest.main()
