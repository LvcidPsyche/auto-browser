"""Unit coverage for the mesh delegation nonce cache (replay defense).

Pure in-memory LRU logic — no network, no crypto, no browser.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.mesh import delegation
from app.mesh.delegation import (
    DelegationError,
    DelegationManager,
    DelegationRejected,
    DelegationReplayError,
    MeshReplayCacheFull,
    _NonceCache,
)
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, DelegationRequest, DelegationResponse, PeerRecord, SignedEnvelope
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import _canonical_bytes, make_envelope
from tests import test_mesh_peer_registration as _registration
from tests.test_mesh_peer_registration import OBSERVE, add_legit_peer, send

mesh = _registration.mesh  # the route fixture: a server, a legit identity, the peers file and a test client


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

    def test_w4_2_a_full_cache_refuses_a_new_nonce_and_never_forgets_an_unexpired_one(self) -> None:
        # The cap evicted the oldest entry whether or not it had expired: accept A, then fill the cache inside A's
        # window, and A was accepted again.
        clock = _Clock()
        cache = _NonceCache(max_size=3, ttl_seconds=60, clock=clock)
        for nonce in ("a", "b", "c"):
            self.assertTrue(cache.check_and_record(nonce))

        with self.assertRaises(MeshReplayCacheFull):
            cache.check_and_record("d")
        self.assertFalse(cache.check_and_record("a"))
        self.assertEqual(list(cache._seen), ["a", "b", "c"])  # "d" was not recorded

        clock.now += 61  # the window has passed for a, b and c
        self.assertTrue(cache.check_and_record("d"))
        self.assertTrue(cache.check_and_record("e"))

    def test_w4_2_at_capacity_an_expired_entry_behind_a_newer_one_is_dropped(self) -> None:
        # Entries are in insertion order, not stamp order: "y" was recorded after "x" with an older stamp.
        clock = _Clock()
        cache = _NonceCache(max_size=2, ttl_seconds=60, clock=clock)
        self.assertTrue(cache.check_and_record("x", stamped_at=1000))
        self.assertTrue(cache.check_and_record("y", stamped_at=950))

        clock.now = 1015  # "y" is 65 s past its stamp, "x" only 15
        self.assertTrue(cache.check_and_record("z", stamped_at=1015))
        self.assertEqual(list(cache._seen), ["x", "z"])
        self.assertFalse(cache.check_and_record("x"))

    def test_w4_2_a_cache_without_a_ttl_keeps_evicting_the_oldest(self) -> None:
        cache = _NonceCache(max_size=2)
        for nonce in ("a", "b", "c"):
            self.assertTrue(cache.check_and_record(nonce))
        self.assertEqual(list(cache._seen), ["b", "c"])

    def test_r5c_17_a_sender_at_its_share_is_refused_and_another_sender_is_not(self) -> None:
        # One sender's unexpired nonces filled the whole cache, and every sender was refused until they expired.
        clock = _Clock()
        cache = _NonceCache(max_size=100, ttl_seconds=60, clock=clock, max_per_owner=2)
        self.assertTrue(cache.check_and_record("a1", owner="flood"))
        self.assertTrue(cache.check_and_record("a2", owner="flood"))

        with self.assertRaises(MeshReplayCacheFull):
            cache.check_and_record("a3", owner="flood")
        self.assertTrue(cache.check_and_record("b1", owner="other"))
        self.assertFalse(cache.check_and_record("a1", owner="flood"))  # still remembered: a replay, not a refusal

        clock.now += 61  # its nonces have expired, and its share is free again
        self.assertTrue(cache.check_and_record("a3", owner="flood"))
        self.assertEqual(dict(cache._held), {"flood": 1})

    def test_r5c_17_an_expired_nonce_behind_a_newer_one_frees_its_senders_share(self) -> None:
        clock = _Clock()
        cache = _NonceCache(max_size=100, ttl_seconds=60, clock=clock, max_per_owner=2)
        self.assertTrue(cache.check_and_record("x", stamped_at=1000, owner="flood"))
        self.assertTrue(cache.check_and_record("y", stamped_at=950, owner="flood"))

        clock.now = 1015  # "y" is 65 s past its stamp, "x" only 15
        self.assertTrue(cache.check_and_record("z", stamped_at=1015, owner="flood"))
        self.assertEqual(list(cache._seen), ["x", "z"])


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


class FloodingPeerTests(unittest.TestCase):
    """R5C-17: one registered peer that sent enough signed envelopes inside a window filled the replay cache, and every
    peer was refused (503) until its nonces expired; a nonce was recorded before its request was even decoded."""

    def test_r5c_17_a_flooding_peer_fills_only_its_share_and_another_peer_is_still_served(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            flooder = NodeIdentity(Path(tmp) / "flooder")
            mesh.peers.add(
                PeerRecord(
                    node_id=flooder.node_id,
                    pubkey_b64=flooder.pubkey_b64,
                    grants=[CapabilityGrant(capability="tool:*")],
                )
            )
            with patch.object(delegation, "_NONCE_CACHE_MAX_PER_SENDER", 2, create=True):
                manager = mesh.manager(timestamp_window=30)
            manager._nonce_cache._max = 3
            manager._started_at = 4000
            with patch.object(delegation.time, "time", return_value=5000):
                for stamp in (5000, 5001):
                    flood = _signed(flooder, mesh.payload, recipient=mesh.server.node_id, timestamp=stamp)
                    self.assertEqual(asyncio.run(manager.receive_inbound(flood)).status, "ok")
                third = _signed(flooder, mesh.payload, recipient=mesh.server.node_id, timestamp=5002)
                with self.assertRaises(MeshReplayCacheFull):
                    asyncio.run(manager.receive_inbound(third))

                self.assertEqual(asyncio.run(manager.receive_inbound(mesh.envelope(5003))).status, "ok")

            self.assertEqual(mesh.runs, 3)

    def test_r5c_17_an_envelope_whose_request_does_not_decode_records_no_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            manager._started_at = 4000
            garbled = _signed(mesh.legit, {"arguments": {}}, recipient=mesh.server.node_id, timestamp=5000)
            with patch.object(delegation.time, "time", return_value=5000):
                with self.assertRaisesRegex(DelegationRejected, "Malformed DelegationRequest"):
                    asyncio.run(manager.receive_inbound(garbled))

            self.assertNotIn(garbled.nonce, manager._nonce_cache._seen)
            self.assertEqual(mesh.runs, 0)


class ReplyReplayTests(unittest.TestCase):
    """delegate() checks a peer's reply against the same clock and nonce cache as an inbound envelope (W2-2, W2-4)."""

    def _delegate(self, mesh: _Mesh, manager: DelegationManager, request: DelegationRequest, reply: SignedEnvelope):
        async def send(*, peer, envelope):
            return reply

        with patch.object(delegation, "send_envelope", send):
            return asyncio.run(manager.delegate(mesh.legit.node_id, request))

    def _reply(self, mesh: _Mesh, request_id: str, timestamp: float) -> SignedEnvelope:
        payload = DelegationResponse(request_id=request_id, status="ok").model_dump(mode="json")
        return _signed(mesh.legit, payload, recipient=mesh.server.node_id, timestamp=timestamp)

    def test_w2_2_a_reply_replayed_after_its_nonce_was_evicted_and_the_clock_stepped_back_is_refused(self) -> None:
        # The reply path read the raw clock while the nonce cache expires by its high-water mark: reply A accepted
        # at 5000, reply X at 5040 evicts A's nonce, the clock steps back to 5025, and A (raw age 25 s) was
        # accepted again.
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            first, second = DelegationRequest(capability="tool:browser.observe"), DelegationRequest(capability="x")
            reply_a = self._reply(mesh, first.request_id, 5000)
            with patch.object(delegation.time, "time", return_value=5000):
                self.assertEqual(self._delegate(mesh, manager, first, reply_a).status, "ok")
            with patch.object(delegation.time, "time", return_value=5040):
                self._delegate(mesh, manager, second, self._reply(mesh, second.request_id, 5040))
            with patch.object(delegation.time, "time", return_value=5025):
                # The same request id, so only the clock can refuse the replay.
                retry = DelegationRequest(request_id=first.request_id, capability="tool:browser.observe")
                with self.assertRaisesRegex(DelegationError, "reply timestamp out of window"):
                    self._delegate(mesh, manager, retry, reply_a)

    def test_w4_2_a_reply_when_the_replay_cache_is_full_raises_a_delegation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            manager._nonce_cache._max = 1
            first, second = DelegationRequest(capability="tool:browser.observe"), DelegationRequest(capability="x")
            with patch.object(delegation.time, "time", return_value=5000):
                self._delegate(mesh, manager, first, self._reply(mesh, first.request_id, 5000))
                with self.assertRaisesRegex(DelegationError, "replay cache is full"):
                    self._delegate(mesh, manager, second, self._reply(mesh, second.request_id, 5001))

    def test_w2_4_a_reply_for_another_request_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            request = DelegationRequest(capability="tool:browser.observe")
            stale = self._reply(mesh, "another-request-id", 5000)
            with patch.object(delegation.time, "time", return_value=5000):
                with self.assertRaisesRegex(DelegationRejected, "request id"):
                    self._delegate(mesh, manager, request, stale)
                matching = self._reply(mesh, request.request_id, 5001)
                self.assertEqual(self._delegate(mesh, manager, request, matching).request_id, request.request_id)


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

    def test_cb3_1_a_replay_after_its_nonce_was_evicted_and_the_clock_stepped_back_is_refused(self) -> None:
        # The window check read the raw clock while the nonce cache expires by its high-water mark: once a later
        # envelope moved the mark past A's stamp + window and evicted A's nonce, a clock stepped back into A's window
        # let A run a second time (1.15.0's size-only cache refused it). Both checks now read one clock that never
        # moves back.
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            manager._started_at = 4000
            first, later = mesh.envelope(5000), mesh.envelope(5040)
            with patch.object(delegation.time, "time", return_value=5000):
                self.assertEqual(asyncio.run(manager.receive_inbound(first)).status, "ok")
            with patch.object(delegation.time, "time", return_value=5040):
                self.assertEqual(asyncio.run(manager.receive_inbound(later)).status, "ok")
            with patch.object(delegation.time, "time", return_value=5025):
                with self.assertRaises(DelegationRejected):
                    asyncio.run(manager.receive_inbound(first))
                # Judged against the high-water mark (5040), not the clock (5025): one stamped 5005 is 35 s old.
                with self.assertRaises(DelegationRejected):
                    asyncio.run(manager.receive_inbound(mesh.envelope(5005)))

            self.assertEqual(mesh.runs, 2)

    def test_w2_11_a_step_back_within_the_window_costs_a_fresh_envelope_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            manager._started_at = 4000
            with patch.object(delegation.time, "time", return_value=5040):
                self.assertEqual(asyncio.run(manager.receive_inbound(mesh.envelope(5040))).status, "ok")
            # 15 s back: an envelope stamped by the stepped-back clock is 15 s behind the mark, inside the window.
            with patch.object(delegation.time, "time", return_value=5025):
                self.assertEqual(asyncio.run(manager.receive_inbound(mesh.envelope(5025))).status, "ok")

            self.assertEqual(mesh.runs, 2)

    def test_w2_11_a_step_back_past_the_window_refuses_fresh_envelopes_until_the_clock_catches_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _Mesh(Path(tmp))
            manager = mesh.manager(timestamp_window=30)
            manager._started_at = 4000
            with patch.object(delegation.time, "time", return_value=5040):
                self.assertEqual(asyncio.run(manager.receive_inbound(mesh.envelope(5040))).status, "ok")
            # 40 s back, more than the window: one stamped by that clock is 40 s behind the mark, and refused.
            with patch.object(delegation.time, "time", return_value=5000):
                with self.assertRaisesRegex(DelegationRejected, "out of window"):
                    asyncio.run(manager.receive_inbound(mesh.envelope(5000)))
            # Once the clock is back within a window of the mark, a fresh envelope runs again.
            with patch.object(delegation.time, "time", return_value=5015):
                self.assertEqual(asyncio.run(manager.receive_inbound(mesh.envelope(5015))).status, "ok")

            self.assertEqual(mesh.runs, 2)

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


def test_w4_2_a_full_replay_cache_answers_503_and_runs_nothing(mesh, caplog) -> None:
    add_legit_peer(mesh, OBSERVE)
    mesh.app.state.delegation_manager._nonce_cache._max = 1
    assert send(mesh, mesh.legit).status_code == 200

    with caplog.at_level(logging.WARNING, logger="app.mesh.delegation"):
        refused = send(mesh, mesh.legit)

    assert refused.status_code == 503
    assert refused.json()["detail"] == "mesh replay cache is full; retry later"
    assert len(mesh.calls) == 1
    assert "replay cache is full" in caplog.text


if __name__ == "__main__":
    unittest.main()
