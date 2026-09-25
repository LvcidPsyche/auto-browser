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
