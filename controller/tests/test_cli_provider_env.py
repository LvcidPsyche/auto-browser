from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter

_CONTROLLER_SECRETS = {
    "API_BEARER_TOKEN": "controller-bearer-SECRET-0123456789abcdef0123456789",
    "STRIPE_SECRET_KEY": "sk_live_SECRET",
    "WITNESS_REMOTE_API_KEY": "witness-SECRET",
    "OPENROUTER_API_KEY": "or-SECRET",
}


def _env_for(adapter_cls, **extra: str) -> dict[str, str]:
    env = {"PATH": "/usr/bin", "LC_ALL": "C.UTF-8", **_CONTROLLER_SECRETS, **extra}
    # clear=True: the real environment (the CI runner's, a developer's) must not merge in, and no assertion
    # below may print the environment, so failures name a variable and never show a value.
    with patch.dict(os.environ, env, clear=True):
        settings = Settings(_env_file=None)
        return adapter_cls(settings).cli_environment()


class CliProviderEnvironmentTests(unittest.TestCase):
    def test_controller_secrets_do_not_reach_the_child(self):
        for adapter_cls in (ClaudeAdapter, OpenAIAdapter, GeminiAdapter):
            env = _env_for(adapter_cls, OPENAI_API_KEY="sk-openai", ANTHROPIC_API_KEY="sk-ant")
            self.assertEqual(sorted(set(env) & set(_CONTROLLER_SECRETS)), [], f"{adapter_cls.__name__} leaked")
            for name in _CONTROLLER_SECRETS:
                self.assertFalse(name in env, f"{adapter_cls.__name__} leaked {name}")

    def test_basics_and_the_providers_own_key_are_kept(self):
        provider_keys = {
            ClaudeAdapter: "ANTHROPIC_API_KEY",
            OpenAIAdapter: "OPENAI_API_KEY",
            GeminiAdapter: "GEMINI_API_KEY",
        }
        for adapter_cls, key in provider_keys.items():
            env = _env_for(adapter_cls, **{key: "the-key"})
            self.assertEqual(env[key], "the-key")
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["LC_ALL"], "C.UTF-8")
            self.assertEqual(env["NO_COLOR"], "1")
            self.assertEqual(env["CI"], "1")

    def test_another_providers_key_is_not_passed(self):
        env = _env_for(ClaudeAdapter, OPENAI_API_KEY="sk-openai", ANTHROPIC_API_KEY="sk-ant")
        self.assertFalse("OPENAI_API_KEY" in env, "the Claude child was given OPENAI_API_KEY")

    def test_cli_home_still_sets_home(self):
        with patch.dict(os.environ, {"CLI_HOME": "/data/cli-home"}, clear=True):
            env = ClaudeAdapter(Settings(_env_file=None)).cli_environment()
        self.assertEqual(env["HOME"], "/data/cli-home")

    def test_passthrough_names_are_passed_as_is(self):
        env = _env_for(
            ClaudeAdapter,
            CLI_PROVIDER_ENV_PASSTHROUGH=" MY_CLI_PROXY , OTHER_VAR ,",
            MY_CLI_PROXY="proxy.example:3128",
            OTHER_VAR="v",
            NOT_LISTED="x",
        )
        self.assertEqual(env["MY_CLI_PROXY"], "proxy.example:3128")
        self.assertEqual(env["OTHER_VAR"], "v")
        self.assertFalse("NOT_LISTED" in env, "a variable outside the allowlist reached the child")


if __name__ == "__main__":
    unittest.main()
