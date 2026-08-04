"""
cdp.passthrough — CDP element intelligence extractor and safe passthrough proxy.

get_element_intelligence() returns computed styles, DOM tree, event listeners,
animations, and asset references for a given CSS selector.

raw_cdp_command() is a constrained passthrough for trusted CDP commands.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Commands allowed through the raw CDP passthrough
_ALLOWED_CDP_COMMANDS = frozenset(
    [
        "Runtime.evaluate",
        "DOM.getDocument",
        "DOM.querySelector",
        "DOM.getAttributes",
        "DOM.getBoxModel",
        "CSS.getComputedStyleForNode",
        "CSS.getMatchedStylesForNode",
        "Animation.getPlaybackRate",
        "Network.getCookies",
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

    def __init__(self, cdp_session: "CDPSession") -> None:  # noqa: F821
        self._cdp = cdp_session

    async def get_element_intelligence(self, selector: str) -> dict[str, Any]:
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
            attrs = dict(zip(attrs_raw["attributes"][::2], attrs_raw["attributes"][1::2]))

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

            # Event listeners via Runtime.evaluate (type names only)
            listeners_result = await self._cdp.send(
                "Runtime.evaluate",
                {
                    "expression": f"""
                    (function() {{
                        const el = document.querySelector({selector!r});
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
            listener_types = listeners_result.get("result", {}).get("value", [])

            # Asset references
            asset_result = await self._cdp.send(
                "Runtime.evaluate",
                {
                    "expression": f"""
                    (function() {{
                        const el = document.querySelector({selector!r});
                        if (!el) return {{}};
                        return {{
                            src: el.src || null,
                            href: el.href || null,
                            currentSrc: el.currentSrc || null,
                            backgroundImage: window.getComputedStyle(el).backgroundImage
                        }};
                    }})()
                """,
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
            result = await self._cdp.send(method, params)
            return result
        except Exception as exc:
            logger.warning("cdp.raw_command error method=%r: %s", method, exc)
            return {"error": "cdp_command_failed", "method": method}

    @classmethod
    async def from_page(cls, page: "Page") -> "CDPPassthrough":  # noqa: F821
        """Create a CDPPassthrough attached to a Playwright Page."""
        cdp_session = await page.context.new_cdp_session(page)
        return cls(cdp_session)
