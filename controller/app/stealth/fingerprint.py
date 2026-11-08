"""
stealth.fingerprint — Browser fingerprint randomization at launch time.

Applied via Playwright's add_init_script() so it runs before any page JS.
Covers: webdriver flag, canvas noise (only; WebGL is left as the browser reports it),
user-agent cycling, timezone/locale consistency per session.
"""

from __future__ import annotations

import hashlib
import random

# ---------------------------------------------------------------------------
# User-agent pool
# ---------------------------------------------------------------------------

# Single authoritative UA pool. config.Settings derives its default USER_AGENT_POOL
# from this list so the context-level UA (sessions service) and the fingerprint
# layer can never drift apart again.
#
# Chromium-engine UAs only: the browser node runs Chromium, and a Firefox/Safari
# UA on a Chromium engine is an engine/UA mismatch — itself a detection signal.
CHROME_UA_POOL = [
    # Chrome 156 on Windows (October 2026 stable, the engine browser-node ships)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    # Chrome 156 on macOS (UA reduction freezes the reported mac version)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    # Chrome 156 on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    # Chrome 155 on Windows (previous stable still common in the wild)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    # Edge 156 on Windows (Chromium engine)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0",
]
_UA_POOL = CHROME_UA_POOL
# NOTE: UA strings go stale fast. Refresh this pool every 2-3 months.
# Stale UAs are themselves a bot signal — Chrome 122 in a 2026 session sticks out.

_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Denver",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Australia/Sydney",
]

_LOCALES = ["en-US", "en-GB", "en-CA", "en-AU", "de-DE", "fr-FR"]


# ---------------------------------------------------------------------------
# Session-stable fingerprint seed
# ---------------------------------------------------------------------------


def _session_seed(session_id: str) -> int:
    """Derive a stable integer seed from a session ID so fingerprint is consistent
    within a session but differs across sessions."""
    return int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# Fingerprint config
# ---------------------------------------------------------------------------


class FingerprintConfig:
    """Per-session fingerprint configuration."""

    def __init__(self, session_id: str, profile: str = "light") -> None:
        rng = random.Random(_session_seed(session_id))
        self.user_agent: str = rng.choice(_UA_POOL)
        self.timezone: str = rng.choice(_TIMEZONES)
        self.locale: str = rng.choice(_LOCALES)
        # Small random canvas noise seed (0-255)
        self.canvas_noise_seed: int = rng.randint(0, 255)
        self.profile = profile

    def playwright_context_kwargs(self) -> dict:
        """Extra kwargs to pass to browser.new_context()."""
        return {
            "user_agent": self.user_agent,
            "timezone_id": self.timezone,
            "locale": self.locale,
        }

    def init_script(self) -> str:
        """JS snippet injected before every page load."""
        noise_seed = self.canvas_noise_seed

        return f"""
(function() {{
    // 1. Mask webdriver flag
    Object.defineProperty(navigator, 'webdriver', {{
        get: () => undefined,
        configurable: true
    }});

    // 2. Mask automation-related properties
    if (window.chrome) {{
        window.chrome.app = window.chrome.app || {{}};
    }}

    // 3. Canvas fingerprint noise: a shift of one up or down on each of a
    // pixel's R, G and B (alpha is left alone), from a hash of the pixel's place
    // on the canvas and the session's seed, so a pixel reads the same in any
    // rectangle. It came from the seed and the byte index, a multiple of 4 at
    // every red byte: every pixel got the same shift, on red only (NAV-24).
    // Patched once on the prototype: getContext('2d') returns the same context
    // every time, so wrapping the instance there stacked another layer of noise
    // (and another stack frame) on every call.
    const _noise_seed = {noise_seed};
    const _seed_hash = Math.imul(_noise_seed, 0xC2B2AE3D);
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
        const imageData = _origGetImageData.apply(this, args);
        const data = imageData.data;
        const width = imageData.width;
        const height = imageData.height;
        // The read's top left corner: a negative width or height reads to the left of or above (sx, sy).
        const left = (args[0] | 0) + Math.min(args[2] | 0, 0);
        const top = (args[1] | 0) + Math.min(args[3] | 0, 0);
        let i = 0;
        for (let row = 0; row < height; row++) {{
            const rowHash = Math.imul(top + row, 0x85EBCA77) ^ _seed_hash;
            for (let column = 0; column < width; column++, i += 4) {{
                // MurmurHash3's finaliser over the place and the seed; bits 0, 1 and 2 pick R's, G's and B's shift.
                let h = Math.imul(left + column, 0x9E3779B1) ^ rowHash;
                h = Math.imul(h ^ (h >>> 16), 0x85EBCA6B);
                h = Math.imul(h ^ (h >>> 13), 0xC2B2AE35);
                h ^= h >>> 16;
                // A Uint8ClampedArray: a shift past 0 or 255 stops there.
                data[i] += (h & 1) * 2 - 1;
                data[i + 1] += (h & 2) - 1;
                data[i + 2] += ((h >>> 1) & 2) - 1;
            }}
        }}
        return imageData;
    }};

    // 4. Remove common automation artifacts
    delete window.__selenium_unwrapped;
    delete window.__webdriver_script_fn;
    delete window.__driver_evaluate;
    delete window.__webdriver_evaluate;
}})();
""".strip()


# ---------------------------------------------------------------------------
# Apply fingerprint to a Playwright BrowserContext
# ---------------------------------------------------------------------------


async def apply_fingerprint(context: "BrowserContext", config: FingerprintConfig) -> None:  # noqa: F821
    """Inject the fingerprint init script into a Playwright BrowserContext."""
    await context.add_init_script(config.init_script())
