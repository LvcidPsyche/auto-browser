"""Turn speculation-rules prefetch off in a browser the controller launched (decision 5, amended).

A prefetch is fetched without DevTools' interception, so the pre-request gate never sees it: the refused host
received the request, and the navigation then committed from the prefetched response with no pause (step 0, m2).
Chromium 151 has no launch switch or CDP command for it; only the preference
``net.network_prediction_options = 2`` (or the policy NetworkPredictionOptions) makes a prefetch ineligible.

This sets the preference in the browser's default profile, the way its own settings page does: open
chrome://settings in the default context, call ``chrome.settingsPrivate.setPref`` there, read it back, close the
tab. Contexts the controller creates afterwards (off-the-record profiles over the default one) inherit it, and
their prefetches become PrefetchNotEligiblePreloadingDisabled (measured, step 0). It is a private settings API:
tests/test_request_gate_hardening_chromium.py goes red when a Chromium build changes it.

Never for an attach mode (CDP_CONNECT_URL, browser.cdp_attach): that is the operator's own "Preload pages"
setting, and the preference syncs to their other devices.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_URL = "chrome://settings/performance"
PREF = "net.network_prediction_options"
# NetworkPredictionOptions: 2 = never predict (no prefetch, no preconnect).
PREFETCH_OFF = 2
_READY = "document.readyState === 'complete' && !!(window.chrome && chrome.settingsPrivate)"
_GET = f"new Promise(r => chrome.settingsPrivate.getPref('{PREF}', p => r(p ? p.value : null)))"
_SET = f"new Promise(r => chrome.settingsPrivate.setPref('{PREF}', {PREFETCH_OFF}, '', r))"
# The whole route: open the page, wait for its API, read, set, read back.
PREF_TIMEOUT_SECONDS = 20.0
_READY_POLL_SECONDS = 0.25
# Each cleanup step (closing the settings tab, detaching). They run as the route is cancelled at its bound, so
# the whole call takes at most PREF_TIMEOUT_SECONDS plus two of these.
CLEANUP_TIMEOUT_SECONDS = 2.0


async def turn_prefetch_off(browser: Any) -> Any:
    """Set the default profile's prediction preference to 2 unless it already is; return the value read back.

    Never raises: a failure is returned as {"error": ...} for /readiness and logged by the caller.
    """
    try:
        return await asyncio.wait_for(_set_pref(browser), timeout=PREF_TIMEOUT_SECONDS)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


async def _set_pref(browser: Any) -> Any:
    cdp = await browser.new_browser_cdp_session()
    replies: dict[int, asyncio.Future] = {}

    def on_message(event: dict[str, Any]) -> None:
        message = json.loads(event.get("message") or "{}")
        waiter = replies.get(message.get("id"))
        if waiter is not None and not waiter.done():
            waiter.set_result(message)

    cdp.on("Target.receivedMessageFromTarget", on_message)
    try:
        target = (await cdp.send("Target.createTarget", {"url": SETTINGS_URL}))["targetId"]
        try:
            session_id = (await cdp.send("Target.attachToTarget", {"targetId": target, "flatten": False}))["sessionId"]
            ids = itertools.count(1)

            async def evaluate(expression: str) -> Any:
                message_id = next(ids)
                waiter = replies[message_id] = asyncio.get_running_loop().create_future()
                params = {"expression": expression, "awaitPromise": True, "returnByValue": True}
                message = {"id": message_id, "method": "Runtime.evaluate", "params": params}
                try:
                    await cdp.send(
                        "Target.sendMessageToTarget", {"sessionId": session_id, "message": json.dumps(message)}
                    )
                    reply = await waiter
                finally:
                    replies.pop(message_id, None)
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply.get("result", {}).get("result", {}).get("value")

            while await evaluate(_READY) is not True:
                await asyncio.sleep(_READY_POLL_SECONDS)
            # Idempotent: a policy (the browser-node image's) or an earlier acquisition already set it.
            if await evaluate(_GET) != PREFETCH_OFF:
                await evaluate(_SET)
            return await evaluate(_GET)
        finally:
            await _bounded_cleanup(
                cdp.send("Target.closeTarget", {"targetId": target}), f"close the settings tab {target}"
            )
    finally:
        await _bounded_cleanup(cdp.detach(), "detach its browser session")


async def _bounded_cleanup(step: Any, what: str) -> None:
    """One cleanup step, bounded on its own: a stalled cleanup adds at most CLEANUP_TIMEOUT_SECONDS to the route's
    PREF_TIMEOUT_SECONDS."""
    try:
        await asyncio.wait_for(step, timeout=CLEANUP_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("prefetch preference: could not %s: %s", what, f"{type(exc).__name__}: {exc}"[:200])
