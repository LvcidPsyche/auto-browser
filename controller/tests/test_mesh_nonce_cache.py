"""Unit coverage for the mesh delegation nonce cache (replay defense).

Pure in-memory LRU logic — no network, no crypto, no browser.
"""

from __future__ import annotations

import unittest

from app.mesh.delegation import _NonceCache


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


if __name__ == "__main__":
    unittest.main()
