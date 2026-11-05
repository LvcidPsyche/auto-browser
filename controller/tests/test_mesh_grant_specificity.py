"""The most specific matching grant applies, whatever order peers.json lists them in."""

from __future__ import annotations

import unittest

from app.mesh.models import CapabilityGrant, DelegationRequest, PeerRecord
from app.mesh.policy import PolicyDenied, PolicyEvaluator

EVIL = {"url": "https://evil.com/"}


def _grant_for(grants: list[CapabilityGrant], capability: str) -> CapabilityGrant:
    peer = PeerRecord(node_id="peer1", pubkey_b64="A" * 44, grants=grants)
    return PolicyEvaluator().evaluate(peer, DelegationRequest(capability=capability, arguments={}))


class GrantSpecificityTests(unittest.TestCase):
    def test_an_exact_grant_listed_after_a_wildcard_still_applies(self) -> None:
        grants = [
            CapabilityGrant(capability="tool:*"),
            CapabilityGrant(capability="tool:browser.observe", url_allowlist=["https://example.com/*"]),
        ]
        peer = PeerRecord(node_id="peer1", pubkey_b64="A" * 44, grants=grants)

        with self.assertRaises(PolicyDenied):
            PolicyEvaluator().evaluate(peer, DelegationRequest(capability="tool:browser.observe", arguments=EVIL))
        # The wildcard still covers every other tool.
        other = PolicyEvaluator().evaluate(
            peer, DelegationRequest(capability="tool:browser.screenshot", arguments=EVIL)
        )
        self.assertEqual(other.capability, "tool:*")

    def test_a_longer_wildcard_prefix_beats_a_shorter_one(self) -> None:
        grants = [
            CapabilityGrant(capability="tool:*"),
            CapabilityGrant(capability="tool:browser:*", max_invocations_per_hour=7),
        ]

        self.assertEqual(_grant_for(grants, "tool:browser:observe").max_invocations_per_hour, 7)
        self.assertEqual(_grant_for(grants, "tool:other").capability, "tool:*")

    def test_ties_resolve_by_file_order(self) -> None:
        first = CapabilityGrant(capability="tool:*", max_invocations_per_hour=1000)
        second = CapabilityGrant(capability="tool:*", max_invocations_per_hour=2000)

        self.assertIs(_grant_for([first, second], "tool:browser.observe"), first)

    def test_no_match_is_still_default_deny(self) -> None:
        with self.assertRaises(PolicyDenied):
            _grant_for([CapabilityGrant(capability="session:*")], "tool:browser.observe")


if __name__ == "__main__":
    unittest.main()
