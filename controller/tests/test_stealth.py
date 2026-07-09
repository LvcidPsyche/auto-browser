"""Deterministic coverage for the stealth timing/motion + fingerprint layers.

No live browser: mouse/keyboard/scroll are exercised against a fake page, and
``asyncio.sleep`` is patched out so the human-timing paths run instantly. Random
draws are seeded where output is asserted.
"""

from __future__ import annotations

import random
import unittest
from unittest.mock import AsyncMock, patch

from app.stealth.fingerprint import FingerprintConfig, _session_seed
from app.stealth.humanizer import (
    PROFILES,
    Humanizer,
    _bezier_points,
    _clamp,
    _gaussian_delay,
)


class HumanizerPureTests(unittest.TestCase):
    def test_clamp(self) -> None:
        self.assertEqual(_clamp(5, 0, 10), 5)
        self.assertEqual(_clamp(-1, 0, 10), 0)
        self.assertEqual(_clamp(99, 0, 10), 10)

    def test_gaussian_delay_is_clamped_and_in_seconds(self) -> None:
        random.seed(1234)
        for _ in range(200):
            delay = _gaussian_delay(180.0, 55.0, 60.0, 600.0)
            self.assertGreaterEqual(delay, 60.0 / 1000.0)
            self.assertLessEqual(delay, 600.0 / 1000.0)

    def test_bezier_points_endpoints_and_count(self) -> None:
        points = _bezier_points(0.0, 0.0, 100.0, 50.0, steps=25, jitter=0.0)
        self.assertEqual(len(points), 26)  # steps + 1
        # With zero jitter the curve starts at the origin and ends at the target.
        self.assertAlmostEqual(points[0][0], 0.0, places=6)
        self.assertAlmostEqual(points[0][1], 0.0, places=6)
        self.assertAlmostEqual(points[-1][0], 100.0, places=6)
        self.assertAlmostEqual(points[-1][1], 50.0, places=6)


class HumanizerProfileTests(unittest.TestCase):
    def test_off_profile_is_inactive(self) -> None:
        self.assertFalse(Humanizer("off").active)
        # Unknown profile also resolves to inactive (PROFILES.get -> None).
        self.assertFalse(Humanizer("does-not-exist").active)

    def test_light_and_aggressive_active(self) -> None:
        self.assertTrue(Humanizer("light").active)
        self.assertTrue(Humanizer("aggressive").active)
        self.assertIsNotNone(PROFILES["aggressive"])


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float]] = []
        self.wheels: list[tuple[int, int]] = []

    async def move(self, x, y):
        self.moves.append((x, y))

    async def click(self, x, y):
        self.clicks.append((x, y))

    async def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []

    async def type(self, ch):
        self.typed.append(ch)


class _FakePage:
    def __init__(self) -> None:
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []

    async def click(self, selector):
        self.clicked.append(selector)

    async def type(self, selector, text):
        self.typed.append((selector, text))


class HumanizerActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._sleep = patch("app.stealth.humanizer.asyncio.sleep", new=AsyncMock())
        self._sleep.start()

    async def asyncTearDown(self) -> None:
        self._sleep.stop()

    async def test_inactive_move_is_direct(self) -> None:
        page = _FakePage()
        hz = Humanizer("off")
        await hz.move_to(page, 100, 200)
        self.assertEqual(page.mouse.moves, [(100, 200)])

    async def test_active_move_follows_bezier_waypoints(self) -> None:
        page = _FakePage()
        hz = Humanizer("light")
        await hz.move_to(page, 300, 300)
        # A curve of mouse_steps+1 waypoints, ending at the target.
        self.assertGreater(len(page.mouse.moves), 2)
        self.assertAlmostEqual(page.mouse.moves[-1][0], 300, delta=1e-6)

    async def test_human_click_moves_clicks_and_settles(self) -> None:
        page = _FakePage()
        hz = Humanizer("light")
        await hz.human_click(page, 50, 60)
        self.assertEqual(page.mouse.clicks, [(50, 60)])

    async def test_inactive_type_uses_page_type(self) -> None:
        page = _FakePage()
        hz = Humanizer("off")
        await hz.human_type(page, "#field", "hi")
        self.assertEqual(page.typed, [("#field", "hi")])

    async def test_active_type_is_per_character(self) -> None:
        page = _FakePage()
        hz = Humanizer("light")
        await hz.human_type(page, "#field", "abc")
        self.assertEqual(page.keyboard.typed, ["a", "b", "c"])
        self.assertEqual(page.clicked, ["#field"])

    async def test_scroll_covers_full_delta(self) -> None:
        random.seed(7)
        page = _FakePage()
        hz = Humanizer("light")
        await hz.human_scroll(page, 200)
        scrolled = sum(dy for _dx, dy in page.mouse.wheels)
        # Net scroll should move down by roughly the requested delta
        # (overshoot corrections net to zero).
        self.assertGreaterEqual(scrolled, 180)

    async def test_inactive_scroll_is_single_wheel(self) -> None:
        page = _FakePage()
        hz = Humanizer("off")
        await hz.human_scroll(page, 120)
        self.assertEqual(page.mouse.wheels, [(0, 120)])


class FingerprintTests(unittest.TestCase):
    def test_session_seed_is_stable_and_distinct(self) -> None:
        self.assertEqual(_session_seed("abc"), _session_seed("abc"))
        self.assertNotEqual(_session_seed("abc"), _session_seed("xyz"))

    def test_config_is_deterministic_per_session(self) -> None:
        a = FingerprintConfig("session-1")
        b = FingerprintConfig("session-1")
        self.assertEqual(a.user_agent, b.user_agent)
        self.assertEqual(a.timezone, b.timezone)
        self.assertEqual(a.canvas_noise_seed, b.canvas_noise_seed)

    def test_config_differs_across_sessions(self) -> None:
        seeds = {FingerprintConfig(f"s-{i}").user_agent for i in range(20)}
        # Not all identical — the pool is actually being sampled.
        self.assertGreater(len(seeds), 1)

    def test_context_kwargs_and_init_script(self) -> None:
        cfg = FingerprintConfig("session-1")
        kwargs = cfg.playwright_context_kwargs()
        self.assertEqual(kwargs["user_agent"], cfg.user_agent)
        self.assertIn("timezone_id", kwargs)
        self.assertIn("locale", kwargs)
        script = cfg.init_script()
        self.assertIn("navigator", script)
        self.assertIn("webdriver", script)
        self.assertIn(str(cfg.canvas_noise_seed), script)


if __name__ == "__main__":
    unittest.main()
