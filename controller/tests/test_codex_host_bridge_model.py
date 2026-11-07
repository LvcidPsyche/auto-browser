"""The Codex host bridge validates the model name it passes to `codex --model` (HARN-44).

A model of `--dangerously-bypass-approvals-and-sandbox` was handed to the CLI as a flag.
"""

from __future__ import annotations

import importlib.util
import socketserver
import sys
import unittest
from pathlib import Path

from app.models import MODEL_NAME_PATTERN

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_host_bridge.py"


def _load_bridge():
    shimmed = not hasattr(socketserver, "UnixStreamServer")
    if shimmed:
        socketserver.UnixStreamServer = socketserver.TCPServer
    try:
        spec = importlib.util.spec_from_file_location("codex_host_bridge_model_under_test", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if shimmed:
            del socketserver.UnixStreamServer


bridge = _load_bridge() if _SCRIPT.is_file() else None
_PAYLOAD = {"prompt": "p", "schema": {"type": "object"}}


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeModelValidationTests(unittest.TestCase):
    def _service(self, default_model: str | None = None):
        return bridge.CodexBridgeService(codex_path="codex-not-installed", default_model=default_model)

    def test_a_bad_model_is_a_400_before_anything_runs(self):
        for model in ("--dangerously-bypass-approvals-and-sandbox", "-x", "a/../b", "x y", "m;ls", "a" * 129):
            with self.subTest(model=model):
                with self.assertRaises(bridge.BridgeError) as caught:
                    self._service().handle_openai_decide({**_PAYLOAD, "model": model})
                self.assertEqual(caught.exception.status_code, 400)

    def test_a_bad_default_model_is_refused_too(self):
        with self.assertRaises(bridge.BridgeError) as caught:
            self._service(default_model="--evil").handle_openai_decide(dict(_PAYLOAD))
        self.assertEqual(caught.exception.status_code, 400)

    def test_vertex_style_models_are_accepted_and_a_leading_at_sign_is_not(self):
        # R-7: `claude-3-5-sonnet@20240620` (Vertex) and `vertex_ai/claude-x@20240620` (LiteLLM) were refused.
        for model in ("claude-3-5-sonnet@20240620", "vertex_ai/claude-x@20240620"):
            with self.subTest(model=model):
                self.assertTrue(bridge.model_name_is_valid(model))
        for model in ("@20240620", "-x@y", "a@..b"):
            with self.subTest(model=model):
                self.assertFalse(bridge.model_name_is_valid(model))
                with self.assertRaises(bridge.BridgeError) as raised:
                    self._service().handle_openai_decide({**_PAYLOAD, "model": model})
                self.assertEqual(raised.exception.status_code, 400)

    def test_the_bridge_pattern_matches_models_py(self):
        self.assertEqual(bridge.MODEL_NAME_PATTERN, MODEL_NAME_PATTERN)
        for name in ("gpt-6.1-sol", "a", "x:y/z", "..", "a..b", "-x", "", "a" * 128, "a" * 129, "@x", "a@..b"):
            with self.subTest(name=name):
                from_models = bool(__import__("re").fullmatch(MODEL_NAME_PATTERN, name)) and ".." not in name
                self.assertEqual(bridge.model_name_is_valid(name), from_models)


if __name__ == "__main__":
    unittest.main()
