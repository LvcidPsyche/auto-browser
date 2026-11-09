from __future__ import annotations

import unittest

from app.rate_limits import SlidingWindowRateLimiter, build_rate_limit_key, is_exempt_path


class RateLimitHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_sliding_window_blocks_after_limit(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10)

        first = await limiter.evaluate("operator:alice", now=0.0)
        second = await limiter.evaluate("operator:alice", now=1.0)
        blocked = await limiter.evaluate("operator:alice", now=2.0)
        reset = await limiter.evaluate("operator:alice", now=11.0)

        self.assertFalse(first.exceeded)
        self.assertEqual(first.remaining, 1)
        self.assertFalse(second.exceeded)
        self.assertEqual(second.remaining, 0)
        self.assertTrue(blocked.exceeded)
        self.assertEqual(blocked.retry_after_seconds, 8)
        self.assertFalse(reset.exceeded)

    async def test_keys_prefer_auth_then_hashed_operator_then_ip(self) -> None:
        auth_key = build_rate_limit_key(
            operator_id_header="X-Operator-Id",
            headers={"X-Operator-Id": "alice", "authorization": "Bearer secret"},
            client_host="127.0.0.1",
        )
        self.assertTrue(auth_key.startswith("auth:"))
        operator_key = build_rate_limit_key(
            operator_id_header="X-Operator-Id",
            headers={"X-Operator-Id": "alice"},
            client_host="127.0.0.1",
        )
        self.assertTrue(operator_key.startswith("operator:"))
        self.assertNotIn("alice", operator_key)
        self.assertEqual(
            build_rate_limit_key(
                operator_id_header="X-Operator-Id",
                headers={},
                client_host="127.0.0.1",
            ),
            "ip:127.0.0.1",
        )

    async def test_padded_spellings_of_one_token_share_a_bucket(self) -> None:
        # Auth ignores spaces and tabs around the token; a bucket per spelling handed a token
        # holder a fresh limit per variant, and let them evict other buckets from the LRU.
        def key(authorization: str) -> str:
            return build_rate_limit_key(
                operator_id_header="X-Operator-Id", headers={"authorization": authorization}, client_host="127.0.0.1"
            )

        for padded in ("Bearer  secret", "Bearer secret ", " Bearer \tsecret\t"):
            with self.subTest(padded=padded):
                self.assertEqual(key(padded), key("Bearer secret"))
        self.assertNotEqual(key("Bearer other"), key("Bearer secret"))

    async def test_the_schemes_case_shares_a_bucket_and_the_tokens_does_not(self) -> None:
        def key(authorization: str) -> str:
            return build_rate_limit_key(
                operator_id_header="X-Operator-Id", headers={"authorization": authorization}, client_host="127.0.0.1"
            )

        for scheme in ("bearer", "BEARER", "bEaReR"):
            with self.subTest(scheme=scheme):
                self.assertEqual(key(f"{scheme} secret"), key("Bearer secret"))
        self.assertNotEqual(key("Bearer SECRET"), key("Bearer secret"))

    async def test_limiter_evicts_oldest_bucket_at_capacity(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60, max_buckets=2)

        await limiter.evaluate("operator:first", now=0.0)
        await limiter.evaluate("operator:second", now=1.0)
        await limiter.evaluate("operator:third", now=2.0)

        self.assertNotIn("operator:first", limiter._events)
        self.assertIn("operator:second", limiter._events)
        self.assertIn("operator:third", limiter._events)

    async def test_limiter_prunes_expired_buckets_before_evicting_active_keys(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=10, window_seconds=10, max_buckets=2)

        await limiter.evaluate("operator:expired", now=0.0)
        await limiter.evaluate("operator:active", now=11.0)
        await limiter.evaluate("operator:new", now=20.0)

        self.assertNotIn("operator:expired", limiter._events)
        self.assertIn("operator:active", limiter._events)
        self.assertIn("operator:new", limiter._events)

    async def test_exempt_paths_match_prefixes(self) -> None:
        exempt = ["/healthz", "/artifacts", "/metrics"]
        self.assertTrue(is_exempt_path("/healthz", exempt))
        self.assertTrue(is_exempt_path("/artifacts/session-1/screenshot.png", exempt))
        self.assertFalse(is_exempt_path("/sessions", exempt))


if __name__ == "__main__":
    unittest.main()
