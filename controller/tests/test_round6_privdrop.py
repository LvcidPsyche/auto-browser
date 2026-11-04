"""1.14.0 round 6: the token pre-check asks the controller, and app.drop_caps fails closed.

- Item 4 (P5#9, E7, O5-3): app.api_token.file_to_read follows ensure_api_credential's own precedence, so entrypoint.sh's
  check never refuses a controller that reads no file, and checks the file it does read: tokenless spellings, its
  loopback and not-production scope, API_TOKEN_FILE from the environment or .env.
- Items 9 and 10 (P5#2, P5#12, E9): drop_caps with a libc whose prctl or capset fails, or lacks them, exits 70 and
  never execs; it resolves the libc already loaded (CDLL(None)), so nothing runs as root before the drop.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import api_token, drop_caps
from app.config import Settings


def _settings(**env) -> Settings:
    return Settings(_env_file=None, **env)


class FileToReadTests(unittest.TestCase):
    def test_an_environment_token_reads_no_file(self) -> None:
        # Tokenless off (tests/__init__.py turns it on), so only the environment token can say "none".
        for env in ({"API_BEARER_TOKEN": "x" * 40}, {"API_BEARER_TOKENS": "ops:" + "y" * 40}):
            with self.subTest(**env):
                settings = _settings(**env, API_ALLOW_TOKENLESS_LOOPBACK="false", API_BIND_SCOPE="loopback")
                self.assertIsNone(api_token.file_to_read(settings))
        self.assertIsNotNone(api_token.file_to_read(_settings(API_ALLOW_TOKENLESS_LOOPBACK="false")))

    def test_tokenless_spellings_on_loopback_read_no_file(self) -> None:
        for value in ("true", "t", "y", "yes", "on", "1"):
            with self.subTest(value=value):
                settings = _settings(API_ALLOW_TOKENLESS_LOOPBACK=value, API_BIND_SCOPE="loopback")
                self.assertIsNone(api_token.file_to_read(settings))

    def test_tokenless_does_not_apply_in_production_or_off_loopback(self) -> None:
        exposed = _settings(API_ALLOW_TOKENLESS_LOOPBACK="true", API_BIND_SCOPE="exposed")
        self.assertIsNotNone(api_token.file_to_read(exposed))
        production = _settings(API_ALLOW_TOKENLESS_LOOPBACK="true", API_BIND_SCOPE="loopback", APP_ENV="production")
        self.assertTrue(production.is_production)
        self.assertIsNotNone(api_token.file_to_read(production))

    def test_api_token_file_from_a_dot_env(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env_file = Path(tempdir) / ".env"
            env_file.write_text("API_TOKEN_FILE=/secrets/token\n", encoding="utf-8")
            # tests/__init__.py sets API_TOKEN_FILE and tokenless loopback: neither may hide the .env here.
            with patch.dict(os.environ, {}, clear=False):
                for name in ("API_TOKEN_FILE", "API_ALLOW_TOKENLESS_LOOPBACK", "API_BEARER_TOKEN", "API_BEARER_TOKENS"):
                    os.environ.pop(name, None)
                settings = Settings(_env_file=str(env_file))
                self.assertEqual(api_token.file_to_read(settings), Path("/secrets/token"))

    def test_the_entry_point_prints_the_path_or_nothing(self) -> None:
        out = io.StringIO()
        with patch.object(api_token, "file_to_read", return_value=Path("/data/api-token")), redirect_stdout(out):
            self.assertEqual(api_token.main(["x", "--file-to-read"]), 0)
        self.assertEqual(out.getvalue().strip(), str(Path("/data/api-token")))
        out = io.StringIO()
        with patch.object(api_token, "file_to_read", return_value=None), redirect_stdout(out):
            self.assertEqual(api_token.main(["x", "--file-to-read"]), 0)
        self.assertEqual(out.getvalue(), "\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(api_token.main(["x"]), 64)


class _FakeLibc:
    def __init__(self, prctl_rc: int = 0, capset_rc: int = 0) -> None:
        self.prctl_rc, self.capset_rc, self.calls = prctl_rc, capset_rc, []

    def prctl(self, *args) -> int:
        self.calls.append("prctl")
        return self.prctl_rc

    def capset(self, *args) -> int:
        self.calls.append("capset")
        return self.capset_rc


class DropCapsFailsClosedTests(unittest.TestCase):
    def _main(self, libc) -> tuple[int, list]:
        execs: list = []
        with (
            patch.object(drop_caps.ctypes, "CDLL", return_value=libc) as cdll,
            patch.object(drop_caps.ctypes, "get_errno", return_value=1),
            patch.object(drop_caps.os, "execvp", side_effect=lambda *a: execs.append(a)),
            redirect_stderr(io.StringIO()),
        ):
            code = drop_caps.main(["x", "--", "uvicorn", "app.main:app"])
        self.cdll_args = cdll.call_args
        return code, execs

    def test_a_failing_prctl_or_capset_exits_70_and_never_execs(self) -> None:
        for libc in (_FakeLibc(prctl_rc=-1), _FakeLibc(capset_rc=-1)):
            with self.subTest(prctl=libc.prctl_rc, capset=libc.capset_rc):
                code, execs = self._main(libc)
                self.assertEqual(code, 70)
                self.assertEqual(execs, [])

    def test_a_libc_without_the_calls_exits_70(self) -> None:
        code, execs = self._main(object())
        self.assertEqual(code, 70)
        self.assertEqual(execs, [])

    def test_the_loaded_libc_is_used_and_success_execs(self) -> None:
        code, execs = self._main(_FakeLibc())
        self.assertEqual(execs, [("uvicorn", ["uvicorn", "app.main:app"])])
        self.assertEqual(self.cdll_args.args, (None,))
        self.assertEqual(self.cdll_args.kwargs, {"use_errno": True})


if __name__ == "__main__":
    unittest.main()
