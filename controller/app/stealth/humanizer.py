"""
stealth.humanizer — Human-like timing and movement wrapper for Playwright actions.

Three profiles:
  off     — no changes, raw Playwright speed
  light   — timing jitter + Bézier mouse (default for 1.0)
  aggressive — full fingerprint noise + tighter human mimicry
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class HumanProfile:
    """Timing and movement parameters."""

    # Click/keystroke timing (ms)
    click_mean_ms: float = 180.0
    click_sigma_ms: float = 55.0
    click_min_ms: float = 60.0
    click_max_ms: float = 600.0

    # Keystroke inter-key delay
    key_mean_ms: float = 75.0
    key_sigma_ms: float = 30.0
    key_min_ms: float = 20.0
    key_max_ms: float = 300.0

    # Mouse movement — Bézier segments
    mouse_steps: int = 25  # points along the curve
    mouse_step_ms: float = 8.0  # ms between steps
    mouse_sigma: float = 12.0  # control-point jitter (px)

    # Scroll
    scroll_step_px: int = 80
    scroll_step_ms: float = 30.0
    scroll_overshoot_prob: float = 0.15

    # Post-action settle
    settle_mean_ms: float = 120.0
    settle_sigma_ms: float = 40.0


PROFILES: dict[str, Optional[HumanProfile]] = {
    "off": None,
    "light": HumanProfile(),
    "aggressive": HumanProfile(
        click_mean_ms=220.0,
        click_sigma_ms=80.0,
        key_mean_ms=90.0,
        key_sigma_ms=40.0,
        mouse_steps=40,
        mouse_sigma=18.0,
        scroll_overshoot_prob=0.25,
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _gaussian_delay(mean_ms: float, sigma_ms: float, lo: float, hi: float) -> float:
    """Return a clamped Gaussian delay in seconds."""
    ms = _clamp(random.gauss(mean_ms, sigma_ms), lo, hi)
    return ms / 1000.0


def _bezier_points(x0: float, y0: float, x1: float, y1: float, steps: int, jitter: float) -> list[Tuple[float, float]]:
    """
    Generate a list of (x, y) waypoints along a cubic Bézier curve between
    two points.  The two control points are jittered to create natural curves.
    """
    cx0 = x0 + (x1 - x0) * 0.25 + random.gauss(0, jitter)
    cy0 = y0 + (y1 - y0) * 0.25 + random.gauss(0, jitter)
    cx1 = x0 + (x1 - x0) * 0.75 + random.gauss(0, jitter)
    cy1 = y0 + (y1 - y0) * 0.75 + random.gauss(0, jitter)

    points = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * x0 + 3 * mt**2 * t * cx0 + 3 * mt * t**2 * cx1 + t**3 * x1
        y = mt**3 * y0 + 3 * mt**2 * t * cy0 + 3 * mt * t**2 * cy1 + t**3 * y1
        points.append((x, y))
    return points


# ---------------------------------------------------------------------------
# Main humanizer class
# ---------------------------------------------------------------------------


class Humanizer:
    """
    Wraps Playwright Page actions with human-like timing and motion.
    Instantiated once per browser session.
    """

    def __init__(self, profile: str = "light") -> None:
        self._profile = PROFILES.get(profile)
        self._last_x: float = 640.0
        self._last_y: float = 400.0

    @property
    def active(self) -> bool:
        return self._profile is not None

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    async def pre_click_delay(self) -> None:
        if not self._profile:
            return
        p = self._profile
        delay = _gaussian_delay(p.click_mean_ms, p.click_sigma_ms, p.click_min_ms, p.click_max_ms)
        await asyncio.sleep(delay)

    async def post_action_settle(self) -> None:
        if not self._profile:
            return
        p = self._profile
        delay = _gaussian_delay(p.settle_mean_ms, p.settle_sigma_ms, 20.0, 400.0)
        await asyncio.sleep(delay)

    async def inter_key_delay(self) -> None:
        if not self._profile:
            return
        p = self._profile
        delay = _gaussian_delay(p.key_mean_ms, p.key_sigma_ms, p.key_min_ms, p.key_max_ms)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Mouse movement
    # ------------------------------------------------------------------

    async def move_to(self, page: "Page", x: float, y: float) -> None:  # noqa: F821
        """Move the mouse from last known position to (x, y) along a Bézier curve."""
        if not self._profile:
            await page.mouse.move(x, y)
            self._last_x, self._last_y = x, y
            return

        p = self._profile
        waypoints = _bezier_points(self._last_x, self._last_y, x, y, p.mouse_steps, p.mouse_sigma)
        for wx, wy in waypoints:
            await page.mouse.move(wx, wy)
            await asyncio.sleep(p.mouse_step_ms / 1000.0)

        self._last_x, self._last_y = x, y

    async def human_click(self, page: "Page", x: float, y: float) -> None:  # noqa: F821
        """Move to position and click with human timing."""
        await self.move_to(page, x, y)
        await self.pre_click_delay()
        await page.mouse.click(x, y)
        await self.post_action_settle()

    # ------------------------------------------------------------------
    # Typing
    # ------------------------------------------------------------------

    async def human_type(self, page: "Page", selector: str, text: str) -> None:  # noqa: F821
        """Type text character by character with Gaussian inter-key delays."""
        if not self._profile:
            await page.type(selector, text)
            return

        await page.click(selector)
        await self.post_action_settle()
        for char in text:
            await page.keyboard.type(char)
            await self.inter_key_delay()

    # ------------------------------------------------------------------
    # Scroll
    # ------------------------------------------------------------------

    async def human_scroll(self, page: "Page", delta_y: int) -> None:  # noqa: F821
        """Scroll with momentum: variable step size, optional overshoot + correct."""
        if not self._profile:
            await page.mouse.wheel(0, delta_y)
            return

        p = self._profile
        remaining = abs(delta_y)
        direction = 1 if delta_y > 0 else -1

        while remaining > 0:
            step = min(remaining, p.scroll_step_px + random.randint(-20, 20))
            await page.mouse.wheel(0, direction * step)
            remaining -= step
            await asyncio.sleep(p.scroll_step_ms / 1000.0 + random.uniform(0, 0.02))

        # Occasional overshoot + correct
        if random.random() < p.scroll_overshoot_prob:
            overshoot = random.randint(10, 40)
            await page.mouse.wheel(0, direction * overshoot)
            await asyncio.sleep(0.15)
            await page.mouse.wheel(0, -direction * overshoot)
