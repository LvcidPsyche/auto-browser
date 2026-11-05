"""The UV command's output is read with a cap, so a runaway command cannot fill the controller's memory."""

from __future__ import annotations

import asyncio
import sys
import unittest
from unittest import mock

from app.harness import TaskContract
from app.harness.verifier.uv import UniversalVerifierAdapter

CONTRACT = TaskContract(
    id="uv-cap",
    goal="uv output is bounded",
    postconditions=[{"kind": "url_contains", "value": "example.com"}],
)
TRACE = {"final_observation": {"url": "https://example.com"}}
VALID = "import json; print(json.dumps({'passed': True, 'confidence': 0.9}))"


class UvOutputCapTests(unittest.IsolatedAsyncioTestCase):
    async def _verify(self, code: str):
        started: list = []
        original = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.append(process)
            return process

        adapter = UniversalVerifierAdapter(command=[sys.executable, "-c", code], timeout_seconds=30)
        with mock.patch("app.harness.verifier.uv.asyncio.create_subprocess_exec", spy):
            result = await adapter.verify(CONTRACT, TRACE)
        return result, started

    async def test_output_over_the_cap_is_unavailable_and_the_child_is_reaped(self) -> None:
        result, started = await self._verify("import sys; sys.stdout.write('x' * 3_000_000)")

        self.assertIsNone(result.passed)
        self.assertFalse(result.details["available"])
        self.assertIn("exceeded", result.notes)
        self.assertIsNotNone(started[0].returncode)

    async def test_a_noisy_stderr_does_not_block_a_valid_answer(self) -> None:
        result, _ = await self._verify("import sys; sys.stderr.write('e' * 3_000_000); " + VALID)

        self.assertIs(result.passed, True)

    async def test_a_normal_answer_is_unchanged(self) -> None:
        result, _ = await self._verify(VALID)

        self.assertIs(result.passed, True)
        self.assertEqual(result.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
