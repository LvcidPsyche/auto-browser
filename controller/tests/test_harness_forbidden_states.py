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

    async def test_common_login_paths_and_login_hosts_are_hits(self) -> None:
        for url in (
            "https://example.com/users/sign_in",
            "https://example.com/log-in",
            "https://example.com/account/logon",
            "https://example.com/authorize",
            "https://idp.example.com/oauth2/authorize?client_id=x",
            "https://login.microsoftonline.com/common/oauth2/authorize",
            "https://login.example.com/",
            "https://signin.example.com/?next=/home",
        ):
            with self.subTest(url=url):
                self.assertEqual(len(await _hits("login_redirect", {"url": url, "text": "welcome"})), 1)

    async def test_common_identity_provider_login_paths_and_an_sso_host_are_hits(self) -> None:
        for url in (
            "https://blog.example.com/wp-login.php?redirect_to=%2F",
            "https://blog.example.com/wp-login",
            "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fmail.google.com",
            "https://accounts.example.com/v3/servicelogin",
            "https://id.example.com/realms/acme/login-actions/authenticate?session_code=x",
            "https://app.example.com/signin-oidc",
            "https://sso.example.com/",
        ):
            with self.subTest(url=url):
                self.assertEqual(len(await _hits("login_redirect", {"url": url, "text": "welcome"})), 1)

    async def test_a_session_id_path_parameter_or_a_hash_route_still_names_the_login_page(self) -> None:
        # 1.15.0's "/login" substring caught these: a Tomcat or Spring first redirect, and hash-routed apps.
        for url in (
            "https://example.com/login;jsessionid=ABC123",
            "https://example.com/app/login;jsessionid=ABC123.node0?next=/",
            "https://app.example.com/#/login",
            "https://app.example.com/#!/signin?next=%2Fhome",
        ):
            with self.subTest(url=url):
                self.assertEqual(len(await _hits("login_redirect", {"url": url, "text": "welcome"})), 1)

    async def test_an_in_page_anchor_is_not_a_route(self) -> None:
        for url in ("https://example.com/pricing#login", "https://example.com/#sign-in-help"):
            with self.subTest(url=url):
                self.assertEqual(await _hits("login_redirect", {"url": url, "text": "welcome"}), [])

    async def test_a_host_that_only_starts_with_a_needle_is_not_a_hit(self) -> None:
        for url in (
            "https://loginhelp.example.com/",
            "https://authors.example.com/jane",
            "https://example.com/authorship",
        ):
            with self.subTest(url=url):
                self.assertEqual(await _hits("login_redirect", {"url": url, "text": "welcome"}), [])

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

    async def test_a_payment_path_with_a_session_id_or_in_a_hash_route_is_a_payment_screen(self) -> None:
        for url in (
            "https://shop.example.com/payment;jsessionid=ABC123",
            "https://shop.example.com/#/checkout/payment",
        ):
            with self.subTest(url=url):
                self.assertEqual(len(await _hits("payment_screen", {"url": url, "text": "Step 3"})), 1)

    async def test_captcha_matches_whole_words(self) -> None:
        self.assertEqual(
            len(await _hits("captcha_screen", {"url": "https://example.com/x", "text": "Solve the CAPTCHA"})), 1
        )
        self.assertEqual(
            await _hits("captcha_screen", {"url": "https://example.com/x", "text": "captchaless sign up"}), []
        )

    async def test_captcha_identifiers_in_page_code_are_hits(self) -> None:
        # Widget code names the captcha with `_` joins or Google's `grecaptcha` global, not as a word.
        for page in (
            {"url": "https://example.com/x", "text": "ok", "dom": "<script>grecaptcha.render('w')</script>"},
            {"url": "https://example.com/x", "text": "ok", "dom": "<img id='captcha_image'>"},
            {"url": "https://example.com/x", "text": "ok", "dom": "<div class='h_captcha'></div>"},
        ):
            with self.subTest(dom=page["dom"]):
                self.assertEqual(len(await _hits("captcha_screen", page)), 1)

    async def test_camel_case_and_widget_captcha_identifiers_are_hits(self) -> None:
        for dom in (
            "<input name='captchaToken'>",
            "<div data-widget='reCaptcha'></div>",
            "<script>if (isCaptchaRequired) show()</script>",
            "<script src='/js/recaptchaV3.js'></script>",
            "<div id='reCAPTCHAWidget'></div>",
            "<script>hcaptcha.render('box')</script>",
            "<script>grecaptcha.execute('key')</script>",
        ):
            with self.subTest(dom=dom):
                self.assertEqual(len(await _hits("captcha_screen", {"url": "https://example.com/x", "dom": dom})), 1)

    async def test_a_word_that_only_contains_captcha_is_still_not_a_hit(self) -> None:
        for text in ("captchaless sign up", "Captchaless sign up", "CAPTCHALESS"):
            with self.subTest(text=text):
                self.assertEqual(await _hits("captcha_screen", {"url": "https://example.com/x", "text": text}), [])

    async def test_a_built_in_phrase_written_in_camel_case_still_hits(self) -> None:
        # Splitting at humps must not cost a hit the unsplit text has ("CloudFlare" is not "Cloud Flare").
        for text in ("Checking your browser: CloudFlare", "Cloudflare Ray ID", "HCaptcha", "ReCAPTCHA"):
            with self.subTest(text=text):
                self.assertEqual(len(await _hits("captcha_screen", {"url": "https://example.com/x", "text": text})), 1)

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
