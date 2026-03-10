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

    async def test_keys_prefer_operator_then_auth_then_ip(self) -> None:
        self.assertEqual(
            build_rate_limit_key(
                operator_id_header="X-Operator-Id",
                headers={"X-Operator-Id": "alice", "authorization": "Bearer secret"},
                client_host="127.0.0.1",
            ),
            "operator:alice",
        )
        auth_key = build_rate_limit_key(
            operator_id_header="X-Operator-Id",
            headers={"authorization": "Bearer secret"},
            client_host="127.0.0.1",
        )
        self.assertTrue(auth_key.startswith("auth:"))
        self.assertEqual(
            build_rate_limit_key(
                operator_id_header="X-Operator-Id",
                headers={},
                client_host="127.0.0.1",
            ),
            "ip:127.0.0.1",
        )

    async def test_exempt_paths_match_prefixes(self) -> None:
        exempt = ["/healthz", "/artifacts", "/metrics"]
        self.assertTrue(is_exempt_path("/healthz", exempt))
        self.assertTrue(is_exempt_path("/artifacts/session-1/screenshot.png", exempt))
        self.assertFalse(is_exempt_path("/sessions", exempt))


if __name__ == "__main__":
    unittest.main()
