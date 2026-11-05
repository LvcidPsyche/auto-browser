import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser.services.bot_challenge import BrowserBotChallengeService
from app.browser_scripts import AB_HREF_MAX, AB_TITLE_MAX, AB_TRUNCATED_MARK


def frame(src: str, visible: bool = True) -> dict:
    return {"src": src, "visible": visible}


class BotChallengeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, url: str, title: str, text: str, iframes: list[dict] | None = None) -> dict | None:
        page = SimpleNamespace(
            url=url,
            evaluate=AsyncMock(return_value={"title": title, "text": text, "iframes": iframes or []}),
        )
        return await BrowserBotChallengeService().check(SimpleNamespace(page=page))

    async def test_detects_bot_challenge_from_page_text(self) -> None:
        # No iframe and an ordinary title: only the text phrase can make this a challenge.
        result = await self._check("https://example.com/login", "Home", "Please verify you are human before continuing")

        assert result is not None
        self.assertTrue(result["bot_challenge_detected"])
        self.assertEqual(result["signal"], "verify you are human")
        self.assertEqual(result["url"], "https://example.com/login")

    async def test_the_report_is_capped_and_names_only_the_frames_that_matched(self) -> None:
        secret = "oauth-code-123"
        matched = "https://www.google.com/recaptcha/api2/anchor?k=" + secret
        result = await self._check(
            "https://example.com/",
            "T" * 1_000_000,
            "",
            [
                frame("https://tracker.example/pixel?token=" + secret),  # visible, but not a provider
                frame("https://challenges.cloudflare.com/x?y=" + "q" * 1_000_000, visible=False),  # not shown
                frame(matched),
                frame("https://user:pw@www.google.com:8443/recaptcha/api2/bframe?" + "z" * 1_000_000),
            ],
        )

        assert result is not None
        self.assertLessEqual(len(result["title"]), AB_TITLE_MAX)
        self.assertEqual(
            result["iframes"],
            ["https://www.google.com/recaptcha/api2/anchor", "https://www.google.com:8443/recaptcha/api2/bframe"],
        )
        self.assertNotIn(secret, str(result))

    async def test_a_matched_frame_path_is_capped_like_a_url(self) -> None:
        result = await self._check(
            "https://example.com/", "Home", "", [frame("https://www.google.com/recaptcha/api2/" + "p" * 1_000_000)]
        )

        assert result is not None
        self.assertEqual(len(result["iframes"][0]), AB_HREF_MAX)
        self.assertTrue(result["iframes"][0].endswith(AB_TRUNCATED_MARK))

    async def test_returns_none_for_normal_page(self) -> None:
        self.assertIsNone(await self._check("https://example.com/dashboard", "Dashboard", "Welcome back"))

    async def test_detects_a_challenge_iframe_alone(self) -> None:
        result = await self._check(
            "https://example.com/", "Home", "", [frame("https://www.google.com/recaptcha/api2/anchor")]
        )
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
            "https://api.funcaptcha.com/fc/gc/",
            "https://api.geetest.com/frame",
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/",
        ]
        for src in frames:
            with self.subTest(frame=src):
                self.assertIsNotNone(await self._check("https://example.com/", "Home", "", [frame(src)]))

    async def test_the_known_interstitial_titles_are_challenges(self) -> None:
        for title in (
            "Just a moment...",
            "  Just a moment… ",
            "Attention Required! | Cloudflare",
            "Access denied | shop.example used Cloudflare to restrict access",
        ):
            with self.subTest(title=title):
                result = await self._check("https://example.com/", title, "")
                assert result is not None
                self.assertTrue(result["bot_challenge_detected"])

    async def test_a_challenge_path_on_the_page_url_is_a_challenge(self) -> None:
        result = await self._check("https://example.com/cdn-cgi/challenge-platform/h/b/orchestrate", "Home", "")
        assert result is not None
        self.assertEqual(result["signal"], "/cdn-cgi/challenge-platform/")

    # Invisible widgets: they load on ordinary login, contact and checkout pages.

    async def test_an_invisible_recaptcha_anchor_is_not_a_challenge(self) -> None:
        src = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=x&size=invisible"
        self.assertIsNone(await self._check("https://shop.example/login", "Log in", "", [frame(src)]))

    async def test_an_invisible_hcaptcha_frame_is_not_a_challenge(self) -> None:
        src = "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox-invisible&id=1"
        self.assertIsNone(await self._check("https://shop.example/login", "Log in", "", [frame(src)]))

    async def test_a_provider_frame_that_is_not_rendered_is_not_a_challenge(self) -> None:
        for src in (
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/",
            "https://www.google.com/recaptcha/api2/anchor?ar=1&k=x",
            "https://geo.captcha-delivery.com/captcha/?initialCid=abc",
        ):
            with self.subTest(frame=src):
                self.assertIsNone(await self._check("https://shop.example/", "Shop", "", [frame(src, visible=False)]))

    # Providers the bare word "captcha" used to catch.

    async def test_a_visible_datadome_frame_is_a_challenge(self) -> None:
        for src in (
            "https://geo.captcha-delivery.com/captcha/?initialCid=abc",
            "https://geo.captcha-delivery.com/interstitial/?initialCid=abc",
        ):
            with self.subTest(frame=src):
                result = await self._check("https://shop.example/", "shop.example", "", [frame(src)])
                assert result is not None
                self.assertEqual(result["signal"], "captcha")

    async def test_a_visible_frame_with_captcha_in_its_hostname_is_a_challenge(self) -> None:
        result = await self._check(
            "https://shop.example/", "Shop", "", [frame("https://captcha.px-cdn.net/PXabc/captcha.js")]
        )
        self.assertIsNotNone(result)

    async def test_captcha_in_a_frames_path_or_query_is_not_a_challenge(self) -> None:
        for src in ("https://embed.example/captcha-help", "https://embed.example/x?ref=hcaptcha.com"):
            with self.subTest(frame=src):
                self.assertIsNone(await self._check("https://shop.example/", "Shop", "", [frame(src)]))

    # Host matching: a provider's docs are not a provider's challenge.

    async def test_a_recaptcha_docs_page_is_not_a_challenge(self) -> None:
        for url in (
            "https://developers.google.com/recaptcha/docs/v3",
            "https://www.hcaptcha.com/pricing",
            "https://docs.hcaptcha.com/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(await self._check(url, "Docs", "How to add it to your site"))

    async def test_a_provider_name_inside_another_host_is_not_a_provider(self) -> None:
        src = "https://challenges.cloudflare.com.example/x"
        self.assertIsNone(await self._check("https://shop.example/", "Shop", "", [frame(src)]))

    # Titles: exact, not a substring.

    async def test_an_ordinary_title_containing_just_a_moment_is_not_a_challenge(self) -> None:
        for title in ("Just a Moment (2024) - IMDb", "Just a moment, loading your cart", "Wait just a moment - survey"):
            with self.subTest(title=title):
                self.assertIsNone(await self._check("https://example.com/", title, ""))
