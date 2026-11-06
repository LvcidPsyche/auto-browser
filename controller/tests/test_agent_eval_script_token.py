"""scripts/agent_eval.py takes the bearer token from the environment; --bearer-token is deprecated (HARN-39).

A command-line argument is readable by every local user (ps, /proc/<pid>/cmdline), which is why the shell
scripts send the token on stdin.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_eval_script_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load() if _SCRIPT.is_file() else None


@unittest.skipIf(script is None, "scripts/ is not shipped in the controller image")
class ResolveBearerTokenTests(unittest.TestCase):
    def test_the_environment_variable_is_used_when_no_flag_is_given(self) -> None:
        self.assertEqual(script.resolve_bearer_token("", {"AUTO_BROWSER_TOKEN": "from-env"}), "from-env")

    def test_no_token_anywhere_is_none(self) -> None:
        self.assertIsNone(script.resolve_bearer_token("", {}))
        self.assertIsNone(script.resolve_bearer_token("", {"AUTO_BROWSER_TOKEN": "  "}))

    def test_the_flag_still_works_but_warns_without_echoing_the_token(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            token = script.resolve_bearer_token("from-flag", {"AUTO_BROWSER_TOKEN": "from-env"})
        self.assertEqual(token, "from-flag")
        self.assertIn("AUTO_BROWSER_TOKEN", stderr.getvalue())
        self.assertNotIn("from-flag", stderr.getvalue())

    def test_the_env_path_is_silent(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            script.resolve_bearer_token("", {"AUTO_BROWSER_TOKEN": "t"})
        self.assertEqual(stderr.getvalue(), "")


@unittest.skipIf(script is None, "scripts/ is not shipped in the controller image")
class MainTokenTests(unittest.TestCase):
    def test_main_builds_the_client_with_the_token_from_the_environment(self) -> None:
        # R-22: the tests above call resolve_bearer_token directly, so reverting main()'s call site passed them all.
        client = Mock()
        with (
            patch.object(script, "ControllerEvalClient", client),
            patch.object(script, "load_cases", return_value=[]),
            patch.object(script, "build_matrix", return_value=[]),
            patch.object(sys, "argv", ["agent_eval.py", "--execute"]),
            patch.dict(os.environ, {"AUTO_BROWSER_TOKEN": "from-env"}, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(script.main(), 0)

        client.assert_called_once()
        self.assertEqual(client.call_args.kwargs["bearer_token"], "from-env")


if __name__ == "__main__":
    unittest.main()
