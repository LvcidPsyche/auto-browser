"""
cdp.passthrough — CDP element intelligence extractor and safe passthrough proxy.

get_element_intelligence() returns computed styles, DOM tree, event listeners,
animations, and asset references for a given CSS selector.

raw_cdp_command() is a constrained passthrough for trusted CDP commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..browser_scripts import AB_ATTR_MAX, AB_MAX_ATTRIBUTES, ELEMENT_ASSETS_SCRIPT, cap_text, cap_url

logger = logging.getLogger(__name__)

# How long one DOM/CSS agent enable or disable may take. CSS.enable waits for the page's stylesheets to load.
CDP_AGENT_TIMEOUT_SECONDS = 5.0

# Attributes whose value is an address: capped like a URL, with the marker.
_URL_ATTRIBUTES = frozenset(
    ["href", "src", "srcset", "action", "formaction", "poster", "data", "cite", "background", "ping", "xlink:href"]
)


def _capped_attributes(pairs: list[str]) -> dict[str, str]:
    """The element's attributes as a dict, bounded: a page can write any number of them, megabytes long."""
    names, values = pairs[::2], pairs[1::2]
    return {
        cap_text(name, AB_ATTR_MAX): cap_url(value) if name.lower() in _URL_ATTRIBUTES else cap_text(value, AB_ATTR_MAX)
        for name, value in list(zip(names, values))[:AB_MAX_ATTRIBUTES]
    }


# Commands allowed through the raw CDP passthrough.
#
# This list is the security boundary of /sessions/{id}/cdp/raw, so it may only
# contain *read-only introspection*. Two entries did not belong and were removed
# (reported privately, 2026-06-17):
#
#   Runtime.evaluate   — runs arbitrary JavaScript in the page context. Since
#                        sessions reuse stored auth profiles, that is cookie
#                        theft and acting as the logged-in user on every site a
#                        profile is authenticated to, plus fetch()-based SSRF to
#                        internal and cloud-metadata addresses, bypassing the
#                        navigation allowlist entirely. An allowlist advertised
#                        as safe cannot contain arbitrary code execution.
#   Network.getCookies — returns cookies for all origins, including HttpOnly
#                        ones, which is the credential material directly.
#
# Removing them does not affect internal element intelligence, which issues
# Runtime.evaluate against the CDP session directly rather than through
# raw_cdp_command, nor browser.eval_js, which uses Playwright's page.evaluate.
# Callers that genuinely need scripted evaluation should use browser.eval_js,
# which is governed and auditable.
_ALLOWED_CDP_COMMANDS = frozenset(
    [
        "DOM.getDocument",
        "DOM.querySelector",
        "DOM.getAttributes",
        "DOM.getBoxModel",
        "CSS.getComputedStyleForNode",
        "CSS.getMatchedStylesForNode",
        "Animation.getPlaybackRate",
        "Performance.getMetrics",
        "Page.getLayoutMetrics",
        "Accessibility.getFullAXTree",
    ]
)

# Domains allowed for element extraction (empty = all allowed)
_ALLOWED_DOMAINS: list[str] = []


def _domain_allowed(url: str) -> bool:
    if not _ALLOWED_DOMAINS:
        return True
    for pattern in _ALLOWED_DOMAINS:
        if fnmatch.fnmatch(url, pattern):
            return True
    return False


class CDPPassthrough:
    """
    Wraps a Playwright CDPSession to provide element intelligence
    and a safe subset of raw CDP commands.
    """

    def __init__(self, cdp_session: "CDPSession", page: "Page | None" = None) -> None:  # noqa: F821
        self._cdp = cdp_session
        # The page the CDP session is attached to; CDP sessions are per page.
        self.page = page
        self._agents_lock = asyncio.Lock()
        # Disables running after a cancelled read; held here so the tasks are not collected mid-run.
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def detach(self) -> None:
        try:
            await self._cdp.detach()
        except Exception as exc:
            logger.debug("cdp session detach failed (page likely closed): %s", exc)

    @contextlib.asynccontextmanager
    async def _css_agents(self) -> AsyncIterator[None]:
        """
        The DOM and CSS agents, on for the length of one read. CSS.* calls fail until CSS.enable has run, and CSS.enable
        needs DOM.enable first. Left on, the CSS agent streams stylesheet events for the life of this cached session,
        so it is switched off again, and a switch that fails on a closed page is only logged. The DOM agent is left as
        DOM.getDocument leaves it: DOM.disable discards every node id the client holds, among them the node_id a read
        returns and the ones a raw chain still means to use. One read at a time: a second read's disable would turn the
        CSS agent off under the first.

        A cancelled read (the route's time limit) does not wait for the disable, which can take as long as a page that
        does not answer: the disable runs in the background, which also keeps the lock until it is done.
        """
        await self._agents_lock.acquire()
        handed_off = False
        try:
            try:
                await self._send_bounded("DOM.enable")
                await self._send_bounded("CSS.enable")
                yield
            except asyncio.CancelledError:
                handed_off = True
                task = asyncio.get_running_loop().create_task(self._disable_css_then_unlock())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                raise
        finally:
            if not handed_off:
                await self._disable_css_then_unlock()

    async def _disable_css_then_unlock(self) -> None:
        try:
            await self._send_bounded("CSS.disable")
        except Exception as exc:
            logger.debug("cdp CSS.disable failed (page likely closed): %s", exc)
        finally:
            self._agents_lock.release()

    async def _send_bounded(self, method: str) -> None:
        await asyncio.wait_for(self._cdp.send(method), CDP_AGENT_TIMEOUT_SECONDS)

    async def get_element_intelligence(self, selector: str) -> dict[str, Any]:
        """Element data for a CSS selector (see _read_element), with the DOM and CSS agents on for the read only."""
        try:
            async with self._css_agents():
                return await self._read_element(selector)
        except Exception as exc:
            logger.warning("cdp.element_intelligence error selector=%r: %s", selector, exc)
            return {"error": "cdp_element_intelligence_failed", "selector": selector}

    async def _read_element(self, selector: str) -> dict[str, Any]:
        """
        Return rich element data for a CSS selector:
        - Computed styles (relevant subset)
        - Bounding box
        - DOM attributes
        - Event listener types (not handlers, for privacy)
        - Active CSS animations
        - Asset references (src, href, background-image URLs)
        """
        try:
            # Get document node
            doc = await self._cdp.send("DOM.getDocument", {"depth": 1})
            doc_node_id = doc["root"]["nodeId"]

            # Find element
            result = await self._cdp.send(
                "DOM.querySelector",
                {
                    "nodeId": doc_node_id,
                    "selector": selector,
                },
            )
            node_id = result.get("nodeId", 0)
            if node_id == 0:
                return {"error": f"Selector not found: {selector!r}"}

            # Gather data in parallel conceptually (sequential here for clarity)
            attrs_raw = await self._cdp.send("DOM.getAttributes", {"nodeId": node_id})
            attrs = _capped_attributes(attrs_raw["attributes"])

            box_model = await self._cdp.send("DOM.getBoxModel", {"nodeId": node_id})
            box = box_model.get("model", {})

            computed = await self._cdp.send("CSS.getComputedStyleForNode", {"nodeId": node_id})
            # Filter to useful style properties
            _STYLE_KEYS = {
                "display",
                "visibility",
                "opacity",
                "position",
                "z-index",
                "width",
                "height",
                "color",
                "background-color",
                "font-size",
                "cursor",
                "pointer-events",
                "overflow",
            }
            styles = {
                prop["name"]: prop["value"] for prop in computed.get("computedStyle", []) if prop["name"] in _STYLE_KEYS
            }

            # The selector goes into JavaScript source, so it is encoded as a JS
            # string literal. It was embedded with Python's repr(), which is not
            # one: it writes non-printable astral characters as \U0001xxxx, an
            # escape JavaScript does not have, so the page queried a different
            # selector than the one DOM.querySelector just matched.
            selector_literal = json.dumps(selector)

            # Event listeners via Runtime.evaluate (type names only)
            listeners_result = await self._cdp.send(
                "Runtime.evaluate",
                {
                    "expression": f"""
                    (function() {{
                        const el = document.querySelector({selector_literal});
                        if (!el) return [];
                        const events = [];
                        // We can only detect inline handlers without getEventListeners (Chrome DevTools only)
                        const attrs = el.attributes;
                        for (let i = 0; i < attrs.length; i++) {{
                            if (attrs[i].name.startsWith('on')) events.push(attrs[i].name.slice(2));
                        }}
                        return events;
                    }})()
                """,
                    "returnByValue": True,
                },
            )
            listener_types = [
                cap_text(name, AB_ATTR_MAX) for name in listeners_result.get("result", {}).get("value", [])
            ][:AB_MAX_ATTRIBUTES]

            # Asset references
            asset_result = await self._cdp.send(
                "Runtime.evaluate",
                {
                    "expression": ELEMENT_ASSETS_SCRIPT.replace("__SELECTOR__", selector_literal),
                    "returnByValue": True,
                },
            )
            assets = asset_result.get("result", {}).get("value", {})

            return {
                "selector": selector,
                "node_id": node_id,
                "attributes": attrs,
                "bounding_box": {
                    "width": box.get("width"),
                    "height": box.get("height"),
                    "content": box.get("content"),
                },
                "computed_styles": styles,
                "event_listener_types": listener_types,
                "assets": {k: v for k, v in assets.items() if v and v != "none"},
            }

        except Exception as exc:
            logger.warning("cdp.element_intelligence error selector=%r: %s", selector, exc)
            return {"error": "cdp_element_intelligence_failed", "selector": selector}

    async def raw_cdp_command(self, method: str, params: dict[str, Any] = None) -> dict[str, Any]:
        """
        Execute a raw CDP command from the allowlist.
        Raises ValueError for disallowed commands.
        """
        params = params or {}
        if method not in _ALLOWED_CDP_COMMANDS:
            raise ValueError(
                f"CDP command {method!r} is not in the allowed list. Allowed: {sorted(_ALLOWED_CDP_COMMANDS)}"
            )
        try:
            if method.startswith("CSS."):
                async with self._css_agents():
                    return await self._cdp.send(method, params)
            return await self._cdp.send(method, params)
        except Exception as exc:
            logger.warning("cdp.raw_command error method=%r: %s", method, exc)
            return {"error": "cdp_command_failed", "method": method}

    @classmethod
    async def from_page(cls, page: "Page") -> "CDPPassthrough":  # noqa: F821
        """Create a CDPPassthrough attached to a Playwright Page."""
        cdp_session = await page.context.new_cdp_session(page)
        return cls(cdp_session, page)
