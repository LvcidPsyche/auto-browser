from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
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

    def test_codex_ca_certificate_reaches_the_codex_child(self):
        # W2-30: Codex reads CODEX_CA_CERTIFICATE (a PEM bundle path) before SSL_CERT_FILE, for a TLS-intercepting
        # proxy or a private root CA; it passed neither this allowlist nor the bridge's.
        codex = _env_for(OpenAIAdapter, CODEX_CA_CERTIFICATE="/etc/ssl/corp-ca.pem")

        self.assertEqual(codex["CODEX_CA_CERTIFICATE"], "/etc/ssl/corp-ca.pem")

    def test_the_codex_automation_key_is_passed_to_the_codex_child_only(self):
        codex = _env_for(OpenAIAdapter, CODEX_API_KEY="codex-key")
        claude = _env_for(ClaudeAdapter, CODEX_API_KEY="codex-key")

        self.assertEqual(codex["CODEX_API_KEY"], "codex-key")
        self.assertFalse("CODEX_API_KEY" in claude, "the Claude child was given CODEX_API_KEY")

    def test_another_providers_key_is_not_passed(self):
        env = _env_for(ClaudeAdapter, OPENAI_API_KEY="sk-openai", ANTHROPIC_API_KEY="sk-ant")
        self.assertFalse("OPENAI_API_KEY" in env, "the Claude child was given OPENAI_API_KEY")

    def test_cli_home_still_sets_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "cli-home")
            with patch.dict(os.environ, {"CLI_HOME": home}, clear=True):
                env = ClaudeAdapter(Settings(_env_file=None)).cli_environment()
        self.assertEqual(env["HOME"], home)

    def test_without_a_cli_home_home_is_left_to_the_cli_environment(self):
        # CLI_HOME unset delegates auth state to the CLI's own environment (describe_cli_readiness says so).
        with patch.dict(os.environ, {"CLI_HOME": "", "HOME": "/home/operator"}, clear=True):
            env = ClaudeAdapter(Settings(_env_file=None)).cli_environment()
        self.assertEqual(env["HOME"], "/home/operator")

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

    def test_an_unwritable_cli_home_falls_back_to_one_private_directory_for_cwd_and_home(self):
        real_mkdir = Path.mkdir
        configured = f"/no-such-cli-home/{uuid.uuid4().hex}"  # warned about once per process: a fresh one

        def mkdir(path, *args, **kwargs):
            if "no-such-cli-home" in str(path):
                raise PermissionError("read-only")
            return real_mkdir(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            environ = {"CLI_HOME": configured, "HOME": tmp, "USERPROFILE": tmp, "LOCALAPPDATA": tmp}
            with patch.dict(os.environ, environ, clear=True):
                adapter = ClaudeAdapter(Settings(_env_file=None))
                with patch.object(Path, "mkdir", mkdir), self.assertLogs("app.providers.base", "WARNING") as logs:
                    folder = adapter.effective_cli_home()
                    env = adapter.cli_environment()
                    again = adapter.effective_cli_home()

            fallback = Path(folder)
            self.assertEqual((env["HOME"], again), (folder, folder))
            self.assertIn(Path(tmp), fallback.parents)  # the user's own directory, never the shared temp dir
            self.assertTrue(fallback.is_dir())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(fallback.stat().st_mode), 0o700)
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn(configured, message)
        self.assertIn(folder, message)
        self.assertIn("CLI_HOME set to a writable directory", message)


if __name__ == "__main__":
    unittest.main()
