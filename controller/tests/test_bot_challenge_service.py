import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser.services.bot_challenge import BrowserBotChallengeService


class BotChallengeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_bot_challenge_from_page_text(self) -> None:
        page = SimpleNamespace(
            url="https://example.com/login",
            evaluate=AsyncMock(
                return_value={
                    "title": "Security Check",
                    "text": "Please verify you are human before continuing",
                    "iframes": ["https://challenge.cloudflare.com/frame"],
                }
            ),
        )
        session = SimpleNamespace(page=page)

        result = await BrowserBotChallengeService().check(session)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["bot_challenge_detected"])
        self.assertEqual(result["url"], "https://example.com/login")

    async def test_returns_none_for_normal_page(self) -> None:
        page = SimpleNamespace(
            url="https://example.com/dashboard",
            evaluate=AsyncMock(return_value={"title": "Dashboard", "text": "Welcome back", "iframes": []}),
        )
        session = SimpleNamespace(page=page)

        self.assertIsNone(await BrowserBotChallengeService().check(session))

    async def test_detects_a_challenge_iframe_alone(self) -> None:
        page = SimpleNamespace(
            url="https://example.com/",
            evaluate=AsyncMock(
                return_value={"title": "Home", "text": "", "iframes": ["https://www.google.com/recaptcha/api2/anchor"]}
            ),
        )
        result = await BrowserBotChallengeService().check(SimpleNamespace(page=page))
        assert result is not None
        self.assertEqual(result["signal"], "captcha")

    async def test_an_unreadable_page_still_checks_the_url(self) -> None:
        page = SimpleNamespace(
            url="https://challenges.cloudflare.com/x",
            evaluate=AsyncMock(side_effect=RuntimeError("navigating")),
        )
        result = await BrowserBotChallengeService().check(SimpleNamespace(page=page))
        assert result is not None
        self.assertEqual(result["signal"], "challenges.cloudflare.com")

    async def _check(self, url: str, title: str, text: str, iframes: list[str] | None = None) -> dict | None:
        page = SimpleNamespace(
            url=url,
            evaluate=AsyncMock(return_value={"title": title, "text": text, "iframes": iframes or []}),
        )
        return await BrowserBotChallengeService().check(SimpleNamespace(page=page))

    async def test_a_blog_post_about_captchas_is_not_a_challenge(self) -> None:
        result = await self._check("https://blog.example/posts/how-captcha-works", "How CAPTCHA works", "An explainer.")
        self.assertIsNone(result)

    async def test_the_words_access_denied_in_a_paragraph_are_not_a_challenge(self) -> None:
        result = await self._check(
            "https://docs.example/errors", "HTTP errors", "A 403 means access denied by the server."
        )
        self.assertIsNone(result)

    async def test_captcha_in_the_query_string_is_not_a_challenge(self) -> None:
        self.assertIsNone(
            await self._check("https://shop.example/search?q=captcha+solver", "Search results", "3 results")
        )

    async def test_a_security_check_link_is_not_a_challenge(self) -> None:
        result = await self._check("https://bank.example/home", "Home", "Run a security check on your account")
        self.assertIsNone(result)

    async def test_each_provider_frame_is_a_challenge(self) -> None:
        frames = [
            "https://www.google.com/recaptcha/api2/anchor",
            "https://www.recaptcha.net/recaptcha/enterprise/anchor",
            "https://newassets.hcaptcha.com/captcha/v1/frame.html",
            "https://client-api.arkoselabs.com/fc/gc/",
            "https://api.geetest.com/frame",
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/",
        ]
        for frame in frames:
            with self.subTest(frame=frame):
                self.assertIsNotNone(await self._check("https://example.com/", "Home", "", [frame]))

    async def test_the_known_interstitial_titles_are_challenges(self) -> None:
        for title in ("Just a moment...", "Attention Required! | Cloudflare"):
            with self.subTest(title=title):
                result = await self._check("https://example.com/", title, "")
                assert result is not None
                self.assertTrue(result["bot_challenge_detected"])
