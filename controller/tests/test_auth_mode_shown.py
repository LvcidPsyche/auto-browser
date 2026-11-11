"""A value set in a provider's *_AUTH_MODE that is no mode is shown by its length, wherever it is shown.

.env.example has each provider's auth mode a few lines under its API key, so a key set on the wrong line is a
mistake to expect. The production start's error bounded what it quotes of such a value, and that check runs under
APP_ENV=production alone: on the default APP_ENV the start said nothing, and the same value was quoted whole in the
provider's readiness detail and returned as its auth_mode. GET /agent/providers hands both to any holder of a
bearer token, browser.list_providers hands them to the agent, and a step with that provider raises the detail.

One rule, in one place (auth_mode_shown): a value of at most MAX_AUTH_MODE_QUOTED characters is a mode or a mistyped
one and is named; a longer one is described by its length. The values below are synthetic stand-ins, not keys.
"""

from __future__ import annotations

import asyncio
import unittest

from app.config import Settings
from app.provider_registry import ProviderRegistry
from app.providers.base import auth_mode_shown
from app.runtime_policy import validate_runtime_policy

PASTED = "sk-" + "K" * 40
PROVIDERS = (("OPENAI_AUTH_MODE", "openai"), ("CLAUDE_AUTH_MODE", "claude"), ("GEMINI_AUTH_MODE", "gemini"))
# A value of exactly the longest length that is quoted, 16, and one a character longer. Neither is a mode. In
# numbers and not from MAX_AUTH_MODE_QUOTED: these are what hold that number where it is.
AT_THE_BOUND = "q" * 16
PAST_THE_BOUND = "q" * 17
PRODUCTION = {
    "APP_ENV": "production",
    "API_BEARER_TOKEN": "p" * 40,
    "SHARE_TOKEN_SECRET": "share-secret-for-tests",
    "REQUIRE_OPERATOR_ID": "true",
    "AUTH_STATE_ENCRYPTION_KEY": "b" * 44,
    "REQUIRE_AUTH_STATE_ENCRYPTION": "true",
    "ALLOWED_HOSTS": "example.com",
    "CONTROLLER_ALLOWED_HOSTS": "controller.example.com",
}


def _surfaces(setting: str, provider: str, value: str) -> dict[str, str]:
    """What each place that shows ``provider``'s auth mode says of ``value``: three on the default APP_ENV, one in
    production (a start that is refused).
    """
    settings = Settings(_env_file=None, API_BIND_SCOPE="loopback", API_BEARER_TOKEN="p" * 40, **{setting: value})
    registry = ProviderRegistry(settings)
    [listed] = [item.model_dump() for item in registry.list() if item.provider == provider]
    try:
        asyncio.run(registry.get(provider).decide(goal="g", observation={}))
    except RuntimeError as exc:
        raised = str(exc)
    else:
        raise AssertionError("a provider whose auth mode is no mode took a step")
    errors = validate_runtime_policy(Settings(_env_file=None, **PRODUCTION, **{setting: value})).errors
    [refused_start] = [error for error in errors if setting in error]
    return {
        "the listing's auth_mode": listed["auth_mode"],
        "the listing's detail": listed["detail"],
        "a step's error": raised,
        "a production start's error": refused_start,
    }


class AuthModeShownTests(unittest.TestCase):
    def test_a_value_too_long_to_be_a_mode_is_shown_nowhere(self) -> None:
        for setting, provider in PROVIDERS:
            for place, text in _surfaces(setting, provider, PASTED).items():
                with self.subTest(setting=setting, place=place):
                    # The value is compared in lower case, as the adapters read it. No part of it: not four of
                    # its characters in a row.
                    for start in range(len(PASTED) - 3):
                        self.assertNotIn(PASTED.lower()[start : start + 4], text.lower())
                    self.assertIn("43 characters, not printed", text)

    def test_a_value_of_the_longest_quoted_length_is_named_and_one_longer_is_not(self) -> None:
        # Any bound from 5 to 42 passed the tests there were: a mistyped mode was tried at 5 characters and a key
        # at 43.
        for setting, provider in PROVIDERS:
            named = _surfaces(setting, provider, AT_THE_BOUND)
            described = _surfaces(setting, provider, PAST_THE_BOUND)
            for place in named:
                with self.subTest(setting=setting, place=place):
                    self.assertIn(AT_THE_BOUND, named[place])
                    self.assertNotIn("not printed", named[place])
                    self.assertNotIn(AT_THE_BOUND, described[place])
                    self.assertIn("17 characters, not printed", described[place])

    def test_a_mode_and_a_mistyped_one_are_named_as_they_were(self) -> None:
        self.assertEqual(
            [auth_mode_shown(mode) for mode in ("api", "cli", "host_bridge", "")], ["api", "cli", "host_bridge", ""]
        )
        surfaces = _surfaces("CLAUDE_AUTH_MODE", "claude", "host_bridge")  # a real mode, on a provider without it
        self.assertEqual(surfaces["the listing's auth_mode"], "host_bridge")
        self.assertEqual(
            surfaces["the listing's detail"], "claude auth mode 'host_bridge' is invalid; expected one of: api, cli"
        )
        self.assertEqual(surfaces["a step's error"], surfaces["the listing's detail"])
        self.assertEqual(
            surfaces["a production start's error"], "CLAUDE_AUTH_MODE=host_bridge is invalid; expected one of: api, cli"
        )

    def test_a_provider_whose_mode_is_one_reports_it_unchanged(self) -> None:
        settings = Settings(_env_file=None, OPENAI_AUTH_MODE=" CLI ", CLAUDE_AUTH_MODE="api", GEMINI_AUTH_MODE="cli")
        modes = {item.provider: item.auth_mode for item in ProviderRegistry(settings).list()}

        self.assertEqual((modes["openai"], modes["claude"], modes["gemini"]), ("cli", "api", "cli"))


if __name__ == "__main__":
    unittest.main()
