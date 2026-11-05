from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from ..page_read import bounded

logger = logging.getLogger(__name__)

# What counts as a challenge is what a challenge page is made of: a frame from a challenge provider, a
# known interstitial title, or the provider's own host or path in the page URL. A bare word such as
# "captcha" or "access denied" in a title, a paragraph or a query string matches ordinary pages too (a blog
# post about CAPTCHAs, a docs page on HTTP 403s), and the check runs after the action has already happened.
# Page scripts are not scanned: invisible reCAPTCHA v3 and Cloudflare's /cdn-cgi/challenge-platform/ script load
# on pages that show no challenge.
#
# Each marker maps to the signal reported for it.
CHALLENGE_PROVIDER_MARKERS = {
    "challenge.cloudflare.com": "challenge.cloudflare.com",
    "challenges.cloudflare.com": "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/": "/cdn-cgi/challenge-platform/",
    "google.com/recaptcha": "captcha",
    "recaptcha.net": "captcha",
    "hcaptcha.com": "captcha",
    "arkoselabs.com": "arkose",
    "funcaptcha.com": "arkose",
    "geetest.com": "captcha",
}
CHALLENGE_PAGE_TITLES = (
    "just a moment",
    "attention required! | cloudflare",
    "used cloudflare to restrict access",
)
CHALLENGE_TEXT_PHRASES = ("verify you are human", "verify you're human")

CHALLENGE_PROBE_SCRIPT = """() => ({
  title: document.title,
  text: document.body?.innerText?.slice(0, 500) || '',
  iframes: Array.from(document.querySelectorAll('iframe')).map((el) => el.src || el.getAttribute('src') || '')
})"""


class BrowserBotChallengeService:
    async def check(self, session: Any) -> dict[str, Any] | None:
        url = session.page.url.lower()
        title = ""
        body_text = ""
        iframe_sources: list[str] = []
        try:
            # One evaluate, not three: this probe runs after every action. Bounded, under the session
            # lock: a probe that times out is checked by the URL alone, like one that failed.
            probe = await bounded(session.page.evaluate(CHALLENGE_PROBE_SCRIPT))
            title = str(probe.get("title") or "").lower()
            body_text = str(probe.get("text") or "").lower()
            iframe_sources = [str(item).lower() for item in probe.get("iframes") or []]
        except Exception as exc:
            # Page may be mid-navigation or already closed; check with what we have.
            logger.debug("bot challenge probe could not read page content: %s", exc)

        parts = urlsplit(url)
        locations = [f"{parts.netloc}{parts.path}", *iframe_sources]
        for marker, signal in CHALLENGE_PROVIDER_MARKERS.items():
            if any(marker in location for location in locations):
                return self._detected(session, signal, title, iframe_sources)
        if any(known in title for known in CHALLENGE_PAGE_TITLES):
            return self._detected(session, "challenge page title", title, iframe_sources)
        for phrase in CHALLENGE_TEXT_PHRASES:
            if phrase in body_text:
                return self._detected(session, phrase, title, iframe_sources)
        return None

    @staticmethod
    def _detected(session: Any, signal: str, title: str, iframe_sources: list[str]) -> dict[str, Any]:
        return {
            "bot_challenge_detected": True,
            "signal": signal,
            "url": session.page.url,
            "title": title,
            "iframes": iframe_sources[:10],
        }
