import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser.services.bot_challenge import BrowserBotChallengeService


class BotChallengeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_bot_challenge_from_page_text(self) -> None:
        page = SimpleNamespace(
            url="https://example.com/login",
            title=AsyncMock(return_value="Security Check"),
            evaluate=AsyncMock(
                side_effect=[
                    "Please verify you are human before continuing",
                    ["https://challenge.cloudflare.com/frame"],
                ]
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
            title=AsyncMock(return_value="Dashboard"),
            evaluate=AsyncMock(side_effect=["Welcome back", []]),
        )
        session = SimpleNamespace(page=page)

        self.assertIsNone(await BrowserBotChallengeService().check(session))

    async def test_ignores_googles_invisible_recaptcha_anchor(self) -> None:
        # Real, unedited shape of the iframe Google embeds on virtually every sign-in/sign-up
        # page as a background risk signal. Nothing renders, there is nothing to click, and no
        # signal fires from the page text either -- an ordinary account-creation form.
        page = SimpleNamespace(
            url="https://accounts.google.com/lifecycle/steps/signup/name",
            title=AsyncMock(return_value="Create your Google Account"),
            evaluate=AsyncMock(
                side_effect=[
                    "Create a Google Account Enter your name First name Last name (optional)",
                    [
                        "https://accounts.google.com/_/bscframe",
                        "https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=6lf-_ekqaaaaao4axrjisahw4_bw76ncfwhln7is&"
                        "co=ahr0chm6ly9hy2nvdw50cy5nb29nbguuy29tojq0mw..&hl=en&v=kemdrjwfxnjgsgdhrslyepwu&size=invisible"
                        "&badge=none&anchor-ms=20000&execute-ms=30000&cb=fr1uwmpiazsh",
                        "",
                    ],
                ]
            ),
        )
        session = SimpleNamespace(page=page)

        self.assertIsNone(await BrowserBotChallengeService().check(session))

    async def test_still_detects_a_visible_recaptcha_checkbox(self) -> None:
        # size=normal (or compact) is the widget actually rendered on screen for the human to
        # tick -- a real challenge, unlike the invisible anchor above.
        page = SimpleNamespace(
            url="https://example.com/signup",
            title=AsyncMock(return_value="Sign up"),
            evaluate=AsyncMock(
                side_effect=[
                    "Sign up",
                    ["https://www.google.com/recaptcha/api2/anchor?size=normal&k=abc"],
                ]
            ),
        )
        session = SimpleNamespace(page=page)

        result = await BrowserBotChallengeService().check(session)
        self.assertIsNotNone(result)
