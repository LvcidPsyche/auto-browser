"""Unit coverage for the mesh delegation nonce cache (replay defense).

Pure in-memory LRU logic — no network, no crypto, no browser.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app.mesh.delegation import DelegationManager, DelegationRejected, _NonceCache
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, PeerRecord
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import make_envelope


class NonceCacheTests(unittest.TestCase):
    def test_first_use_is_accepted_replay_is_rejected(self) -> None:
        cache = _NonceCache(max_size=8)
        self.assertTrue(cache.check_and_record("n1"))
        # Same nonce a second time is a replay.
        self.assertFalse(cache.check_and_record("n1"))
        # A different nonce is still fine.
        self.assertTrue(cache.check_and_record("n2"))

    def test_lru_eviction_forgets_oldest(self) -> None:
        cache = _NonceCache(max_size=3)
        for nonce in ("a", "b", "c"):
            self.assertTrue(cache.check_and_record(nonce))
        # Recording a 4th evicts the oldest ("a").
        self.assertTrue(cache.check_and_record("d"))
        # "a" was evicted, so it is accepted again (no longer remembered).
        self.assertTrue(cache.check_and_record("a"))
        # "c"/"d" are still remembered as replays.
        self.assertFalse(cache.check_and_record("c"))
        self.assertFalse(cache.check_and_record("d"))

    def test_capacity_never_exceeds_max(self) -> None:
        cache = _NonceCache(max_size=5)
        for i in range(50):
            cache.check_and_record(f"nonce-{i}")
        self.assertLessEqual(len(cache._seen), 5)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class NonceExpiryTests(unittest.TestCase):
    def test_a_nonce_is_forgotten_after_the_ttl_and_remembered_before_it(self) -> None:
        clock = _Clock()
        cache = _NonceCache(max_size=100, ttl_seconds=60, clock=clock)
        self.assertTrue(cache.check_and_record("n1"))

        clock.now += 59
        self.assertFalse(cache.check_and_record("n1"))
        clock.now += 2
        self.assertTrue(cache.check_and_record("n1"))

    def test_expired_entries_are_dropped_on_each_check(self) -> None:
        clock = _Clock()
        cache = _NonceCache(max_size=100, ttl_seconds=60, clock=clock)
        for index in range(10):
            cache.check_and_record(f"old-{index}")

        clock.now += 61
        cache.check_and_record("new")

        self.assertEqual(list(cache._seen), ["new"])

    def test_the_size_cap_still_applies_inside_the_ttl(self) -> None:
        cache = _NonceCache(max_size=3, ttl_seconds=60, clock=_Clock())
        for nonce in ("a", "b", "c", "d"):
            cache.check_and_record(nonce)

        self.assertEqual(list(cache._seen), ["b", "c", "d"])


class RestartReplayTests(unittest.TestCase):
    def test_an_envelope_stamped_before_this_process_started_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server, legit = NodeIdentity(root / "server"), NodeIdentity(root / "legit")
            peers = PeerRegistryFile(root / "peers.json")
            peers.add(
                PeerRecord(
                    node_id=legit.node_id,
                    pubkey_b64=legit.pubkey_b64,
                    grants=[CapabilityGrant(capability="tool:*")],
                )
            )
            payload = {"capability": "tool:browser.observe", "arguments": {}, "session_id": ""}
            captured = make_envelope(legit, payload, recipient_node_id=server.node_id)
            time.sleep(0.05)

            async def gateway(tool, arguments, session_id):
                return {"tool": tool}

            manager = DelegationManager(server, peers, tool_gateway=gateway)

            with self.assertRaisesRegex(DelegationRejected, "before this node started"):
                asyncio.run(manager.receive_inbound(captured))
            fresh = make_envelope(legit, payload, recipient_node_id=server.node_id)
            self.assertEqual(asyncio.run(manager.receive_inbound(fresh)).status, "ok")


if __name__ == "__main__":
    unittest.main()
