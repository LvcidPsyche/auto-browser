from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

BOT_CHALLENGE_SIGNALS = (
    "challenge.cloudflare.com",
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
    "captcha",
    "recaptcha",
    "hcaptcha",
    "arkose",
    "unusual activity",
    "suspicious activity",
    "verify you're human",
    "verify you are human",
    "security check",
    "access denied",
    "bot detected",
)


def _is_invisible_captcha_widget(src: str) -> bool:
    """True for an invisible reCAPTCHA/hCaptcha *anchor* iframe: the slot the widget would
    render into if it ever became visible, present on countless ordinary Google/other pages
    as a background risk signal and interacted with by no one. `size=invisible` (or `size=`
    absent on an explicitly non-"normal"/"compact" anchor) means nothing is shown and there is
    nothing for a human to do -- flagging it made every Google sign-in/sign-up page permanently
    unusable. A real, solvable challenge renders in a *different* frame (bframe/checkbox,
    Cloudflare's challenge page, hCaptcha's challenge iframe) which this still catches."""
    if "size=invisible" in src:
        return True
    return "recaptcha" in src and "/anchor" in src and "size=normal" not in src and "size=compact" not in src


class BrowserBotChallengeService:
    async def check(self, session: Any) -> dict[str, Any] | None:
        url = session.page.url.lower()
        title = ""
        body_text = ""
        iframe_sources: list[str] = []
        try:
            title = (await session.page.title()).lower()
            body_text = (await session.page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''")).lower()
            iframe_sources = [
                item.lower()
                for item in (
                    await session.page.evaluate(
                        "() => Array.from(document.querySelectorAll('iframe')).map((el) => el.src || el.getAttribute('src') || '')"
                    )
                )
            ]
        except Exception as exc:
            # Page may be mid-navigation or already closed; check with what we have.
            logger.debug("bot challenge probe could not read page content: %s", exc)

        visible_iframe_sources = [src for src in iframe_sources if not _is_invisible_captcha_widget(src)]
        combined = f"{url} {title} {body_text} {' '.join(visible_iframe_sources)}"
        for signal in BOT_CHALLENGE_SIGNALS:
            if signal in combined:
                return {
                    "bot_challenge_detected": True,
                    "signal": signal,
                    "url": session.page.url,
                    "title": title,
                    "iframes": iframe_sources[:10],
                }
        return None
