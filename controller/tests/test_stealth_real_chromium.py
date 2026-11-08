"""The stealth scripts, run in a real Chromium.

`STEALTH_INIT_SCRIPT` was a bare arrow function. `add_init_script` injects its
source as written, so the function was defined and never called: with
STEALTH_ENABLED=true no patch ran, and the syntax gate stayed green because the
script parsed. Only a page can say whether a patch took effect, so these tests
read the patched values back from one.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.browser_manager import BrowserManager
from app.browser_scripts import apply_stealth
from app.stealth.fingerprint import FingerprintConfig
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

BLANK = "data:text/html,<p>x</p>"

# What each patch in STEALTH_INIT_SCRIPT claims to do, read back from the page.
BASE_PATCHES = """() => ({
  webdriver: navigator.webdriver,
  chromeRuntime: !!(window.chrome && window.chrome.runtime),
  pluginCount: navigator.plugins.length,
  languages: navigator.languages,
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory,
  colorDepth: screen.colorDepth,
  glGetParameterPatched: !WebGLRenderingContext.prototype.getParameter.toString().includes('[native code]'),
})"""

# One pixel drawn, exported and read back from the PNG: the export noise flips the low bit of its red channel. The
# patched toDataURL reads as native code (NAV-25), so this is how a page tells it is patched.
EXPORTED_PIXEL = """async () => {
  const canvas = document.createElement('canvas');
  canvas.width = 1; canvas.height = 1;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 1, 1);
  const image = new Image();
  image.src = canvas.toDataURL();
  await image.decode();
  const copy = document.createElement('canvas');
  copy.width = 1; copy.height = 1;
  const copyContext = copy.getContext('2d');
  copyContext.drawImage(image, 0, 0);
  return Array.from(copyContext.getImageData(0, 0, 1, 1).data);
}"""
EXPORT_NOISED_PIXEL = [101, 100, 100, 255]

# A canvas exported before anything asked for its context: whether it can still take each kind of context.
CONTEXT_AFTER_EXPORT = """() => Object.fromEntries(['webgl', 'bitmaprenderer'].map((kind) => {
  const canvas = document.createElement('canvas');
  canvas.toDataURL();
  return [kind, Boolean(canvas.getContext(kind))];
}))"""

# What the canvas patches show of themselves, read the ways a detection script reads a function's source.
CANVAS_PATCH_SOURCES = """() => [
  HTMLCanvasElement.prototype.toDataURL.toString(),
  Function.prototype.toString.call(HTMLCanvasElement.prototype.getContext),
  Function.prototype.toString.toString(),
  HTMLCanvasElement.prototype.toDataURL.length,
  HTMLCanvasElement.prototype.getContext.length,
  'prototype' in HTMLCanvasElement.prototype.toDataURL,
]"""

# An export that throws as it reads its type: how often the type was read, and the canvas's pixel afterwards.
THROWING_EXPORT = """() => {
  const canvas = document.createElement('canvas');
  canvas.width = 1; canvas.height = 1;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 1, 1);
  let calls = 0;
  let error = null;
  try {
    canvas.toDataURL({ toString() { calls += 1; throw new Error('bad type'); } });
  } catch (e) {
    error = e.message;
  }
  return { error, calls, pixel: Array.from(ctx.getImageData(0, 0, 1, 1).data) };
}"""

NOTIFICATIONS_QUERY = """async () => {
  const result = await navigator.permissions.query({ name: 'notifications' });
  return { plainObject: Object.getPrototypeOf(result) === Object.prototype, state: result.state };
}"""


async def _launch(playwright):
    return await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)


@requires_chromium
def test_apply_stealth_patches_a_real_page() -> None:
    from playwright.async_api import async_playwright

    async def run() -> tuple[dict, dict, list[int]]:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                page = await browser.new_page()
                await apply_stealth(page)
                await page.goto(BLANK)
                return (
                    await page.evaluate(BASE_PATCHES),
                    await page.evaluate(NOTIFICATIONS_QUERY),
                    await page.evaluate(EXPORTED_PIXEL),
                )
            finally:
                await browser.close()

    patches, notifications, exported = asyncio.run(run())

    assert patches["webdriver"] is None, patches
    assert patches["chromeRuntime"] is True, patches
    assert patches["pluginCount"] == 3, patches
    assert patches["languages"] == ["en-US", "en"], patches
    assert patches["hardwareConcurrency"] == 8, patches
    assert patches["deviceMemory"] == 8, patches
    assert patches["colorDepth"] == 24, patches
    assert exported == EXPORT_NOISED_PIXEL
    assert patches["glGetParameterPatched"] is True, patches
    assert notifications["plainObject"] is True, notifications


async def _native_and_stealthy(script: str) -> tuple[object, object]:
    """`script`'s answer on a page without the base stealth script, then on one with it, in one browser."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            native = await browser.new_page()
            await native.goto(BLANK)
            stealthy = await browser.new_page()
            await apply_stealth(stealthy)
            await stealthy.goto(BLANK)
            return await native.evaluate(script), await stealthy.evaluate(script)
        finally:
            await browser.close()


@requires_chromium
def test_exporting_a_canvas_leaves_it_free_to_take_any_context() -> None:
    """NAV-25: toDataURL asked for the canvas's 2d context to add its noise, which gives a canvas that has no context
    yet a 2d one, so a later getContext('webgl') or getContext('bitmaprenderer') on it returned null."""
    native, stealthy = asyncio.run(_native_and_stealthy(CONTEXT_AFTER_EXPORT))

    assert native["bitmaprenderer"] is True, native
    assert stealthy == native


@requires_chromium
def test_the_canvas_patches_read_as_native_code() -> None:
    """NAV-25: the patched toDataURL showed its source to any script that called toString on it."""
    native, stealthy = asyncio.run(_native_and_stealthy(CANVAS_PATCH_SOURCES))

    assert native[:3] == [
        "function toDataURL() { [native code] }",
        "function getContext() { [native code] }",
        "function toString() { [native code] }",
    ], native
    assert stealthy == native


@requires_chromium
def test_an_export_that_throws_is_made_once_and_leaves_the_canvas_as_it_was() -> None:
    """Review of c5 (Codex): toDataURL put its noise on the canvas, and an export that threw (a type whose toString
    throws) was made again from its catch with the noise still there, so the page saw its type read twice and kept
    a changed pixel."""
    native, stealthy = asyncio.run(_native_and_stealthy(THROWING_EXPORT))

    assert native == {"error": "bad type", "calls": 1, "pixel": [100, 100, 100, 255]}, native
    assert stealthy == native


# Shared by the exports below: a 1x1 canvas holding rgb(100, 100, 100), a red of 200 drawn at its one pixel, and the
# pixel an exported URL holds, read back from the PNG.
EXPORT_HELPERS = """
  const newCanvas = () => {
    const canvas = document.createElement('canvas');
    canvas.width = 1; canvas.height = 1;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'rgb(100, 100, 100)';
    ctx.fillRect(0, 0, 1, 1);
    return { canvas, ctx };
  };
  const drawRed200 = (ctx) => { ctx.fillStyle = 'rgb(200, 100, 100)'; ctx.fillRect(0, 0, 1, 1); };
  const pixelOf = (ctx) => Array.from(ctx.getImageData(0, 0, 1, 1).data);
  const exportedPixel = async (url) => {
    const image = new Image();
    image.src = url;
    await image.decode();
    const copy = document.createElement('canvas');
    copy.width = 1; copy.height = 1;
    const copyContext = copy.getContext('2d');
    copyContext.drawImage(image, 0, 0);
    return Array.from(copyContext.getImageData(0, 0, 1, 1).data);
  };
"""

# A type whose toString draws at (0, 0) and then throws: the page's error, and what it drew is kept.
EXPORT_TYPE_DRAWS_THEN_THROWS = (
    "async () => {"
    + EXPORT_HELPERS
    + """
  const { canvas, ctx } = newCanvas();
  let error = null;
  try {
    canvas.toDataURL({ toString() { drawRed200(ctx); throw new Error('bad type'); } });
  } catch (e) {
    error = e.message;
  }
  return { error, pixel: pixelOf(ctx) };
}"""
)

# A type whose toString draws at (0, 0) and returns a type: what it drew is kept, and the export shows it.
EXPORT_TYPE_DRAWS_THEN_ANSWERS = (
    "async () => {"
    + EXPORT_HELPERS
    + """
  const { canvas, ctx } = newCanvas();
  const url = canvas.toDataURL({ toString() { drawRed200(ctx); return 'image/png'; } });
  return { pixel: pixelOf(ctx), exported: await exportedPixel(url) };
}"""
)

# A type that is a Symbol: a TypeError, as for any DOMString, and the canvas as it was.
EXPORT_SYMBOL_TYPE = (
    "async () => {"
    + EXPORT_HELPERS
    + """
  const { canvas, ctx } = newCanvas();
  let error = null;
  try {
    canvas.toDataURL(Symbol('type'));
  } catch (e) {
    error = e.name;
  }
  return { error, pixel: pixelOf(ctx) };
}"""
)

# A canvas with width and height accessors of its own that throw, which the native export never reads.
EXPORT_WITH_OWN_THROWING_SIZE = (
    "async () => {"
    + EXPORT_HELPERS
    + """
  const { canvas } = newCanvas();
  for (const name of ['width', 'height']) {
    Object.defineProperty(canvas, name, { get() { throw new Error('own ' + name); } });
  }
  let error = null;
  let exported = null;
  try {
    exported = await exportedPixel(canvas.toDataURL());
  } catch (e) {
    error = e.message;
  }
  return { error, exported };
}"""
)

# Not a canvas the patch gave a 2d context: the native's brand check comes before it converts the type.
EXPORT_ON_A_NON_CANVAS = """() => {
  let calls = 0;
  let error = null;
  try {
    HTMLCanvasElement.prototype.toDataURL.call({}, { toString() { calls += 1; return 'image/png'; } });
  } catch (e) {
    error = e.name;
  }
  return { error, calls };
}"""


@requires_chromium
def test_a_type_that_draws_then_throws_leaves_what_it_drew_and_the_pages_error() -> None:
    """Review of c5 (Codex): the type's toString ran inside the native export, after the pixel was noised, and the
    finally then put the old pixel back over what the page had drawn. The type is converted before the canvas is
    touched now, so no page code runs between the change and its undoing."""
    native, stealthy = asyncio.run(_native_and_stealthy(EXPORT_TYPE_DRAWS_THEN_THROWS))

    assert native == {"error": "bad type", "pixel": [200, 100, 100, 255]}, native
    assert stealthy == native


@requires_chromium
def test_a_type_that_draws_then_answers_leaves_what_it_drew_and_the_export_shows_it() -> None:
    native, stealthy = asyncio.run(_native_and_stealthy(EXPORT_TYPE_DRAWS_THEN_ANSWERS))

    assert native == {"pixel": [200, 100, 100, 255], "exported": [200, 100, 100, 255]}, native
    # The export is noised as any other is, and the canvas is not.
    assert stealthy == {"pixel": [200, 100, 100, 255], "exported": [201, 100, 100, 255]}, stealthy


@requires_chromium
def test_a_symbol_type_is_a_type_error_and_leaves_the_canvas_alone() -> None:
    native, stealthy = asyncio.run(_native_and_stealthy(EXPORT_SYMBOL_TYPE))

    assert native == {"error": "TypeError", "pixel": [100, 100, 100, 255]}, native
    assert stealthy == native


@requires_chromium
def test_an_own_size_accessor_that_throws_does_not_stop_the_export() -> None:
    """Review of c5 (Codex): the patch read this.width and this.height, which a page's own accessor can make throw
    where the native export, which reads neither, does not. They are read through the prototype's getters now."""
    native, stealthy = asyncio.run(_native_and_stealthy(EXPORT_WITH_OWN_THROWING_SIZE))

    assert native == {"error": None, "exported": [100, 100, 100, 255]}, native
    assert stealthy == {"error": None, "exported": [101, 100, 100, 255]}, stealthy


@requires_chromium
def test_an_export_on_a_non_canvas_is_refused_before_its_type_is_read() -> None:
    native, stealthy = asyncio.run(_native_and_stealthy(EXPORT_ON_A_NON_CANVAS))

    assert native == {"error": "TypeError", "calls": 0}, native
    assert stealthy == native


# A locator wait on an element that never appears fails after the default timeout.
SESSION_TIMEOUT_MS = 700


async def _stealth_and_timeout(page) -> dict:
    """Which stealth patches this page has, and how long it waits for a missing element."""
    patches = await page.evaluate(
        "() => ({ webdriver: navigator.webdriver, runtime: !!(window.chrome && window.chrome.runtime) })"
    )
    try:
        await page.locator("#never-there").wait_for()
        patches["timeout"] = None
    except Exception as exc:  # the Playwright timeout names the limit it applied
        patches["timeout"] = str(exc).splitlines()[0]
    return patches


@requires_chromium
class SessionPagesShareStealthAndTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Stealth and the default timeout were set on the first page only, so tabs
    opened later and popups ran unpatched and waited Playwright's 30 s default."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = _settings(Path(self.tmp.name))
        settings.stealth_enabled = True
        settings.action_timeout_ms = SESSION_TIMEOUT_MS
        self.manager = BrowserManager(settings)
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_first_page_new_tab_and_popup_all_get_both(self) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
                session = self.manager.sessions[(await self.manager.create_session(name="tabs"))["id"]]
                try:
                    first = session.page
                    await first.goto(BLANK)
                    tab = await session.context.new_page()
                    await tab.goto(BLANK)
                    async with session.context.expect_page() as popup_info:
                        await first.evaluate("() => { window.open('about:blank'); }")
                    popup = await popup_info.value
                    results = {
                        "first page": await _stealth_and_timeout(first),
                        "new tab": await _stealth_and_timeout(tab),
                        "popup": await _stealth_and_timeout(popup),
                    }
                finally:
                    await session.context.close()
            finally:
                await browser.close()

        for name, seen in results.items():
            with self.subTest(page=name):
                self.assertIsNone(seen["webdriver"], seen)
                self.assertTrue(seen["runtime"], seen)
                self.assertIn(f"Timeout {SESSION_TIMEOUT_MS}ms", seen["timeout"] or "", seen)


# --- the fingerprint canvas noise -------------------------------------------------

CANVAS_SESSION_ID = "canvas-noise-session"

# Draw known pixels, then read them back after `calls` more getContext('2d') calls.
READ_AFTER_CALLS = """(calls) => {
  const canvas = document.createElement('canvas');
  canvas.width = 8; canvas.height = 8;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 8, 8);
  for (let i = 0; i < calls; i++) canvas.getContext('2d');
  return Array.from(canvas.getContext('2d').getImageData(0, 0, 2, 2).data);
}"""

# Same canvas, then toDataURL `calls` times (it fetches the 2d context itself), then read.
READ_AFTER_TO_DATA_URL = """(calls) => {
  const canvas = document.createElement('canvas');
  canvas.width = 8; canvas.height = 8;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 8, 8);
  for (let i = 0; i < calls; i++) canvas.toDataURL();
  return Array.from(ctx.getImageData(0, 0, 2, 2).data);
}"""


def _noised_once(pixels: list[int]) -> bool:
    """Whether a read of rgb(100, 100, 100) is as one pass of the noise leaves it: R, G and B one off, alpha kept."""
    colours = [value for index, value in enumerate(pixels) if index % 4 != 3]
    return all(abs(value - 100) == 1 for value in colours) and pixels[3::4] == [255] * (len(pixels) // 4)


# An 8x8 canvas of one colour read whole, then some of its pixels read again: one alone, a 2x2 block, and the same
# block from its far corner (a negative width and height read up and to the left).
READ_PIXELS_SEVERAL_WAYS = """() => {
  const canvas = document.createElement('canvas');
  canvas.width = 8; canvas.height = 8;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 8, 8);
  const read = (...rect) => Array.from(ctx.getImageData(...rect).data);
  return { whole: read(0, 0, 8, 8), single: read(5, 3, 1, 1), block: read(4, 2, 2, 2), flipped: read(6, 4, -2, -2) };
}"""
OTHER_SESSION_ID = "another-canvas-noise-session"

# A pixel read with coordinates that are objects, each answering its number once and throwing after, as a value read
# twice would; then the same pixel read with numbers.
READ_WITH_COORDINATE_OBJECTS = """() => {
  const canvas = document.createElement('canvas');
  canvas.width = 8; canvas.height = 8;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 8, 8);
  const calls = [0, 0, 0, 0];
  const once = (index, value) => ({ valueOf() {
    calls[index] += 1;
    if (calls[index] > 1) throw new Error('read twice');
    return value;
  } });
  let objects;
  try {
    objects = Array.from(ctx.getImageData(once(0, 5), once(1, 3), once(2, 1), once(3, 1)).data);
  } catch (e) {
    objects = e.message;
  }
  return { objects, numbers: Array.from(ctx.getImageData(5, 3, 1, 1).data), calls };
}"""

# A canvas of one grey read as float16 values, and the kind of array they came in.
READ_FLOAT16 = """() => {
  const canvas = document.createElement('canvas');
  canvas.width = 2; canvas.height = 2;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(128, 128, 128)';
  ctx.fillRect(0, 0, 2, 2);
  const image = ctx.getImageData(0, 0, 2, 2, { pixelFormat: 'rgba-float16' });
  return { kind: Object.prototype.toString.call(image.data), values: Array.from(image.data) };
}"""
GREY_128_AS_FLOAT16 = 0.501953125


async def _read_on_fingerprinted_pages(script: str, *configs: FingerprintConfig) -> list:
    """`script`'s answer on a page with each config's fingerprint script, one context each."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            seen = []
            for config in configs:
                context = await browser.new_context()
                await context.add_init_script(config.init_script())
                page = await context.new_page()
                await page.goto(BLANK)
                seen.append(await page.evaluate(script))
                await context.close()
            return seen
        finally:
            await browser.close()


@requires_chromium
def test_canvas_noise_differs_by_pixel_channel_and_seed_and_holds_for_a_pixel_however_it_is_read() -> None:
    """NAV-24: the noise came from the seed and the byte index, a multiple of 4 at every red byte, so every pixel got
    the same shift and only red changed: two possible outputs in all. Each of R, G and B now gets its own one-off shift
    from a hash of the pixel's place on the canvas and the seed, so a pixel reads the same in any rectangle."""
    config, other = FingerprintConfig(CANVAS_SESSION_ID, "light"), FingerprintConfig(OTHER_SESSION_ID, "light")
    assert config.canvas_noise_seed != other.canvas_noise_seed

    seen, seen_other = asyncio.run(_read_on_fingerprinted_pages(READ_PIXELS_SEVERAL_WAYS, config, other))

    whole = seen["whole"]
    assert _noised_once(whole), whole
    shifts = [tuple(whole[index + channel] - 100 for index in range(0, len(whole), 4)) for channel in range(3)]
    assert all(set(channel) == {-1, 1} for channel in shifts), shifts
    assert len(set(shifts)) == 3, shifts

    def pixel(x: int, y: int) -> list[int]:
        start = (y * 8 + x) * 4
        return whole[start : start + 4]

    assert seen["single"] == pixel(5, 3)
    assert seen["block"] == pixel(4, 2) + pixel(5, 2) + pixel(4, 3) + pixel(5, 3)
    assert seen["flipped"] == seen["block"]
    assert seen_other["whole"] != whole


@requires_chromium
def test_the_noise_reads_each_coordinate_once_as_the_native_read_does() -> None:
    """Review of c5 (Codex): the noise read the coordinates again after the native getImageData had, so a coordinate
    object's valueOf ran twice: one that answers once failed the read, and one that answers differently could put the
    noise at another place."""
    (seen,) = asyncio.run(_read_on_fingerprinted_pages(READ_WITH_COORDINATE_OBJECTS, FingerprintConfig("s", "light")))

    assert seen["calls"] == [1, 1, 1, 1], seen
    assert seen["objects"] == seen["numbers"], seen
    assert _noised_once(seen["numbers"]), seen


@requires_chromium
def test_a_read_of_float_values_is_left_as_it_is() -> None:
    """Review of c5 (Codex): the noise shifted each channel by one, a step in a read of bytes; in a read of float16
    values (pixelFormat "rgba-float16") that is the whole 0-1 range, and grey read as -0.5 or 1.5."""
    (seen,) = asyncio.run(_read_on_fingerprinted_pages(READ_FLOAT16, FingerprintConfig("s", "light")))

    if seen["kind"] != "[object Float16Array]":
        pytest.skip(f"this Chromium reads no float16 pixels: {seen['kind']}")
    assert seen["values"] == [GREY_128_AS_FLOAT16, GREY_128_AS_FLOAT16, GREY_128_AS_FLOAT16, 1] * 4, seen


@requires_chromium
def test_canvas_noise_is_applied_once_however_often_getcontext_is_called() -> None:
    from playwright.async_api import async_playwright

    config = FingerprintConfig(CANVAS_SESSION_ID, "light")

    async def run() -> dict[str, list[int]]:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                context = await browser.new_context()
                await context.add_init_script(config.init_script())
                page = await context.new_page()
                await page.goto(BLANK)
                return {
                    "after 1 call": await page.evaluate(READ_AFTER_CALLS, 1),
                    "after 1000 calls": await page.evaluate(READ_AFTER_CALLS, 1000),
                    # Unpatched, the wrapper chain overflows the stack around 20,000 calls.
                    "after 25000 calls": await page.evaluate(READ_AFTER_CALLS, 25_000),
                }
            finally:
                await browser.close()

    seen = asyncio.run(run())

    assert _noised_once(seen["after 1 call"]), seen
    assert seen["after 1000 calls"] == seen["after 1 call"]
    assert seen["after 25000 calls"] == seen["after 1 call"]


@requires_chromium
def test_fingerprint_script_leaves_webgl_untouched() -> None:
    from playwright.async_api import async_playwright

    config = FingerprintConfig(CANVAS_SESSION_ID, "light")

    async def run() -> bool:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                context = await browser.new_context()
                await context.add_init_script(config.init_script())
                page = await context.new_page()
                await page.goto(BLANK)
                return await page.evaluate(
                    "() => WebGLRenderingContext.prototype.getParameter.toString().includes('[native code]')"
                )
            finally:
                await browser.close()

    assert asyncio.run(run()) is True


@requires_chromium
def test_both_stealth_scripts_on_one_page_do_not_fight() -> None:
    """STEALTH_PROFILE=light adds the fingerprint script to the base one. Both patch
    navigator.webdriver, canvas reads and WebGL; neither may throw or undo the other,
    and reading a canvas back must not drift because toDataURL was called."""
    from playwright.async_api import async_playwright

    config = FingerprintConfig(CANVAS_SESSION_ID, "light")

    async def run() -> tuple[dict, list[int], list[str]]:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                context = await browser.new_context()
                await apply_stealth(context)  # as create_session does, first
                await context.add_init_script(config.init_script())  # then the session hook
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(BLANK)
                patches = await page.evaluate(BASE_PATCHES)
                return patches, await page.evaluate(READ_AFTER_TO_DATA_URL, 50), errors
            finally:
                await browser.close()

    patches, canvas_after_exports, errors = asyncio.run(run())

    assert errors == []
    assert patches["webdriver"] is None, patches
    assert patches["chromeRuntime"] is True, patches
    assert patches["pluginCount"] == 3, patches
    assert patches["hardwareConcurrency"] == 8, patches
    # toDataURL must leave the canvas as it found it: one noise pass on read, no more.
    assert _noised_once(canvas_after_exports), canvas_after_exports
