"""Sentinel forbidden states read the URL and status, and match page text by whole words."""

from __future__ import annotations

import unittest

from app.harness import ForbiddenState, Postcondition, TaskContract
from app.harness.verifier.programmatic import ProgrammaticVerifier


async def _hits(kind: str, observation: dict, *, value: str = "") -> list[str]:
    contract = TaskContract(
        id="sentinel",
        goal="Reject a sentinel state",
        postconditions=[Postcondition(kind="url_contains", value="example.com", required=False)],
        forbidden_states=[ForbiddenState(kind=kind, value=value)],
    )
    result = await ProgrammaticVerifier().verify(contract, {"final_observation": observation, "evidence": ["trace"]})
    return result.forbidden_state_hits


class LoginRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_marketing_page_that_says_sign_in_is_not_a_login_redirect(self) -> None:
        page = {
            "url": "https://example.com/pricing",
            "text": "Sign in to your account, log in anytime, login required for the admin area. Authenticate with SSO.",
            "status_code": 200,
        }

        self.assertEqual(await _hits("login_redirect", page), [])

    async def test_the_url_path_decides(self) -> None:
        for url in (
            "https://example.com/login",
            "https://example.com/users/sign-in?next=/home",
            "https://example.com/SignIn.html",
            "https://example.com/auth/callback",
        ):
            with self.subTest(url=url):
                self.assertEqual(len(await _hits("login_redirect", {"url": url, "text": "welcome"})), 1)

    async def test_a_path_that_only_starts_with_a_needle_is_not_a_hit(self) -> None:
        for url in ("https://example.com/author/jane", "https://example.com/blog/login-tips"):
            with self.subTest(url=url):
                self.assertEqual(await _hits("login_redirect", {"url": url, "text": "welcome"}), [])

    async def test_a_401_or_403_status_is_a_hit_and_a_200_is_not(self) -> None:
        for code in (401, 403, "401"):
            with self.subTest(code=code):
                page = {"url": "https://example.com/account", "status_code": code}
                self.assertEqual(len(await _hits("login_redirect", page)), 1)
        self.assertEqual(await _hits("login_redirect", {"url": "https://example.com/a", "status_code": 200}), [])

    async def test_the_callers_value_is_matched_against_the_url_only(self) -> None:
        page = {"url": "https://sso.example.com/start", "text": "welcome"}
        self.assertEqual(len(await _hits("login_redirect", page, value="sso.example.com")), 1)
        text_only = {"url": "https://example.com/home", "text": "go to sso.example.com"}
        self.assertEqual(await _hits("login_redirect", text_only, value="sso.example.com"), [])


class PaymentAndCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_word_payment_alone_is_not_a_payment_screen(self) -> None:
        page = {"url": "https://example.com/faq", "text": "Payment terms are net 30. We accept invoices."}
        self.assertEqual(await _hits("payment_screen", page), [])

    async def test_payment_phrases_match_as_whole_words(self) -> None:
        self.assertEqual(
            len(await _hits("payment_screen", {"url": "https://example.com/c", "text": "Enter card number"})), 1
        )
        self.assertEqual(
            len(await _hits("payment_screen", {"url": "https://example.com/c", "text": "Pay by Credit Card here"})), 1
        )
        self.assertEqual(await _hits("payment_screen", {"url": "https://example.com/c", "text": "accredit cards"}), [])

    async def test_a_payment_path_is_a_payment_screen(self) -> None:
        page = {"url": "https://example.com/checkout/payment", "text": "Step 3"}
        self.assertEqual(len(await _hits("payment_screen", page)), 1)

    async def test_captcha_matches_whole_words(self) -> None:
        self.assertEqual(
            len(await _hits("captcha_screen", {"url": "https://example.com/x", "text": "Solve the CAPTCHA"})), 1
        )
        self.assertEqual(
            await _hits("captcha_screen", {"url": "https://example.com/x", "text": "captchaless sign up"}), []
        )

    async def test_the_callers_value_is_a_substring_whatever_its_edge_characters(self) -> None:
        # Only the built-in phrases match as whole words; the caller's own value keeps its substring match, so a
        # value that starts or ends with a non-word character (a path) still finds itself next to a letter.
        cases = (
            ("/billing", "https://shop.com/billing"),
            ("checkout/", "https://shop.com/checkout/pay"),
            ("/checkout", "https://shop.com/checkout"),
            ("/Billing", "https://shop.com/billing/history"),
        )
        for kind in ("payment_screen", "captcha_screen"):
            for value, url in cases:
                with self.subTest(kind=kind, value=value, url=url):
                    self.assertEqual(len(await _hits(kind, {"url": url, "text": "Step 3"}, value=value)), 1)
            with self.subTest(kind=kind, absent=True):
                self.assertEqual(
                    await _hits(kind, {"url": "https://shop.com/home", "text": "Step 3"}, value="/billing"), []
                )


if __name__ == "__main__":
    unittest.main()
