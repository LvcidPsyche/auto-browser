"""Unit coverage for the mesh delegation nonce cache (replay defense).

Pure in-memory LRU logic — no network, no crypto, no browser.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.mesh import delegation
from app.mesh.delegation import DelegationManager, DelegationRejected, DelegationReplayError, _NonceCache
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, PeerRecord, SignedEnvelope
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import _canonical_bytes, make_envelope


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


def _signed(identity: NodeIdentity, payload: dict, *, recipient: str, timestamp: float) -> SignedEnvelope:
    """An envelope built and signed by hand, stamped when the test says."""
    envelope = SignedEnvelope(
        sender_node_id=identity.node_id, recipient_node_id=recipient, payload=payload, timestamp=timestamp
    )
    envelope.signature_b64 = base64.b64encode(identity.sign(_canonical_bytes(envelope))).decode()
    return envelope


class _Mesh:
    """A server node, a granted peer, and a gateway that counts what ran."""

    def __init__(self, root: Path) -> None:
        self.server, self.legit = NodeIdentity(root / "server"), NodeIdentity(root / "legit")
        self.peers = PeerRegistryFile(root / "peers.json")
        self.peers.add(
            PeerRecord(
                node_id=self.legit.node_id,
                pubkey_b64=self.legit.pubkey_b64,
                grants=[CapabilityGrant(capability="tool:*")],
            )
        )
        self.payload = {"capability": "tool:browser.observe", "arguments": {}, "session_id": ""}
        self.runs = 0

    async def gateway(self, tool, arguments, session_id):
        self.runs += 1
        return {"tool": tool}

    def manager(self, **kwargs) -> DelegationManager:
        return DelegationManager(self.server, self.peers, tool_gateway=self.gateway, **kwargs)

    def envelope(self, timestamp: float) -> SignedEnvelope:
        return _signed(self.legit, self.payload, recipient=self.server.node_id, timestamp=timestamp)


class RestartReplayTests(unittest.TestCase):
    def test_an_envelope_stamped_ahead_runs_once_across_a_restart(self) -> None:
        # R-16: the window lets an envelope be stamped up to one window ahead, and the restart guard refused only one
        # stamped before the start, so an envelope accepted before a restart ran again after it.
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            before = mesh.manager()
            before._started_at -= 100  # a node that has been up a while
            ahead = mesh.envelope(time.time() + 20)
            self.assertEqual(asyncio.run(before.receive_inbound(ahead)).status, "ok")

            restarted = mesh.manager()
            with self.assertRaises(DelegationRejected):
                asyncio.run(restarted.receive_inbound(ahead))

            self.assertEqual(mesh.runs, 1)

    def test_an_envelope_stamped_just_past_one_window_after_the_start_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager()
            moment = manager._started_at + manager._timestamp_window + 1
            envelope = mesh.envelope(moment)

            with patch.object(delegation.time, "time", return_value=moment):
                self.assertEqual(asyncio.run(manager.receive_inbound(envelope)).status, "ok")

    def test_a_wall_clock_step_back_does_not_free_a_nonce_still_inside_the_window(self) -> None:
        # The nonce expired by monotonic age: once that passed, a wall clock stepped back put the same envelope
        # inside the window again, and it ran a second time.
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=0.5)
            stamped = time.time()
            manager._started_at = stamped - 100
            envelope = mesh.envelope(stamped)
            with patch.object(delegation.time, "time", return_value=stamped):
                self.assertEqual(asyncio.run(manager.receive_inbound(envelope)).status, "ok")
                time.sleep(1.1)  # past twice the window on the monotonic clock; the wall clock reads `stamped` again
                with self.assertRaises(DelegationReplayError):
                    asyncio.run(manager.receive_inbound(envelope))

            self.assertEqual(mesh.runs, 1)

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
            # One stamped more than a window after the start, as a sender's retry is, runs.
            later = manager._started_at + manager._timestamp_window + 1
            fresh = _signed(legit, payload, recipient=server.node_id, timestamp=later)
            with patch.object(delegation.time, "time", return_value=later):
                self.assertEqual(asyncio.run(manager.receive_inbound(fresh)).status, "ok")


if __name__ == "__main__":
    unittest.main()
