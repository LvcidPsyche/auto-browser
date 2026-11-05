from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from ..page_read import bounded

logger = logging.getLogger(__name__)

# What counts as a challenge is what a challenge page is made of: a rendered, visible frame from a challenge
# provider, a known interstitial title, or a challenge host or path in the page URL. A bare word such as
# "captcha" or "access denied" in a title, a paragraph or a query string matches ordinary pages too (a blog
# post about CAPTCHAs, a docs page on HTTP 403s), and the check runs after the action has already happened.
# Page scripts are not scanned: invisible reCAPTCHA v3 and Cloudflare's /cdn-cgi/challenge-platform/ script load
# on pages that show no challenge. The same widgets also insert a frame (reCAPTCHA v3's badge, an invisible
# hCaptcha or Turnstile frame), so a frame counts only when the probe saw it rendered and visible and its address
# does not say it is invisible. Providers are matched by the parsed host (equal, or a subdomain), never by a
# substring of the whole address, so a provider's docs or marketing pages are not challenges.
#
# Host (or parent domain) of a frame that is a challenge, mapped to the signal reported for it.
CHALLENGE_FRAME_HOSTS = {
    "challenge.cloudflare.com": "challenge.cloudflare.com",
    "challenges.cloudflare.com": "challenges.cloudflare.com",
    "recaptcha.net": "captcha",
    "hcaptcha.com": "captcha",
    "arkoselabs.com": "arkose",
    "funcaptcha.com": "arkose",
    "geetest.com": "captcha",
    "captcha-delivery.com": "captcha",
}
# Hosts that serve a challenge as the page itself. A provider's marketing and docs hosts (www.hcaptcha.com,
# developers.google.com) are left out.
CHALLENGE_PAGE_HOSTS = (
    "challenge.cloudflare.com",
    "challenges.cloudflare.com",
    "recaptcha.net",
    "captcha-delivery.com",
)
GOOGLE_HOSTS = ("google.com", "www.google.com")
RECAPTCHA_PATH = "/recaptcha/"
CLOUDFLARE_CHALLENGE_PATH = "/cdn-cgi/challenge-platform/"
# Any other visible frame whose hostname contains this is a challenge too (DataDome's captcha-delivery.com,
# PerimeterX's captcha.px-cdn.net): the old bare "captcha" test, held to the hostname.
CHALLENGE_HOST_WORD = "captcha"
# A frame address that says the widget is not shown: reCAPTCHA v2 and v3 `size=invisible`, hCaptcha `checkbox-invisible`.
INVISIBLE_FRAME_MARKERS = ("size=invisible", "checkbox-invisible")
# Compared with the whole title, lowercased and stripped.
CHALLENGE_PAGE_TITLES = frozenset(
    {
        "just a moment...",
        "just a moment…",
        "attention required! | cloudflare",
    }
)
# Cloudflare's block page: "Access denied | <site> used Cloudflare to restrict access".
CHALLENGE_TITLE_SUFFIX = " used cloudflare to restrict access"
CHALLENGE_TEXT_PHRASES = ("verify you are human", "verify you're human")

CHALLENGE_PROBE_SCRIPT = """() => ({
  title: document.title,
  text: document.body?.innerText?.slice(0, 500) || '',
  iframes: Array.from(document.querySelectorAll('iframe')).map((el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return {
      src: el.src || el.getAttribute('src') || '',
      visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'
    };
  })
})"""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _split(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        return (parts.hostname or ""), parts.path
    except ValueError:
        return "", ""


def _frame_signal(src: str) -> str | None:
    if any(marker in src for marker in INVISIBLE_FRAME_MARKERS):
        return None
    host, path = _split(src)
    for domain, signal in CHALLENGE_FRAME_HOSTS.items():
        if _host_matches(host, domain):
            return signal
    if host in GOOGLE_HOSTS and path.startswith(RECAPTCHA_PATH):
        return "captcha"
    if CHALLENGE_HOST_WORD in host:
        return "captcha"
    return None


def _page_signal(url: str) -> str | None:
    host, path = _split(url)
    for domain in CHALLENGE_PAGE_HOSTS:
        if _host_matches(host, domain):
            return CHALLENGE_FRAME_HOSTS.get(domain, "captcha")
    if host in GOOGLE_HOSTS and path.startswith(RECAPTCHA_PATH):
        return "captcha"
    if path.startswith(CLOUDFLARE_CHALLENGE_PATH):
        return CLOUDFLARE_CHALLENGE_PATH
    return None


class BrowserBotChallengeService:
    async def check(self, session: Any) -> dict[str, Any] | None:
        url = session.page.url.lower()
        title = ""
        body_text = ""
        iframe_sources: list[str] = []
        visible_frames: list[str] = []
        try:
            # One evaluate, not three: this probe runs after every action. Bounded, under the session
            # lock: a probe that times out is checked by the URL alone, like one that failed.
            probe = await bounded(session.page.evaluate(CHALLENGE_PROBE_SCRIPT))
            title = str(probe.get("title") or "").lower()
            body_text = str(probe.get("text") or "").lower()
            for item in probe.get("iframes") or []:
                src = str(item.get("src") or "").lower()
                iframe_sources.append(src)
                if item.get("visible"):
                    visible_frames.append(src)
        except Exception as exc:
            # Page may be mid-navigation or already closed; check with what we have.
            logger.debug("bot challenge probe could not read page content: %s", exc)

        signal = _page_signal(url) or next(filter(None, map(_frame_signal, visible_frames)), None)
        if signal:
            return self._detected(session, signal, title, iframe_sources)
        title = title.strip()
        if title in CHALLENGE_PAGE_TITLES or title.endswith(CHALLENGE_TITLE_SUFFIX):
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
