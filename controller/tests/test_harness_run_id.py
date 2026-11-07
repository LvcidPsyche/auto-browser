"""A harness run id is a path component: it must not reach outside runs_root (HARN-14)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.harness.iterate import HarnessRunStore
from app.tool_inputs import HarnessGetStatusInput, HarnessGetTraceInput

_TRAVERSAL = ("../x", "..", "a/b", "a\\b", "/abs", "C:\\x", ".hidden", "x" * 121, "")


class HarnessRunIdInputTests(unittest.TestCase):
    def test_traversal_ids_are_refused_at_the_tool_boundary(self):
        for run_id in _TRAVERSAL:
            for model in (HarnessGetStatusInput, HarnessGetTraceInput):
                with self.subTest(run_id=run_id, model=model.__name__):
                    with self.assertRaises(ValueError):
                        model(run_id=run_id)

    def test_generated_ids_are_accepted(self):
        self.assertEqual(HarnessGetStatusInput(run_id="0123456789ab").run_id, "0123456789ab")


class HarnessRunStoreRunIdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = HarnessRunStore(Path(self._tmp.name))

    def test_run_dir_refuses_ids_that_could_leave_runs_root(self):
        for run_id in _TRAVERSAL:
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    self.store.run_dir(run_id)

    def test_get_treats_a_bad_id_as_not_found(self):
        with self.assertRaises(KeyError):
            self.store.get("../../outside")

    def test_run_dir_stays_under_runs_root_for_a_generated_id(self):
        path = self.store.run_dir("0123456789ab")
        self.assertEqual(path.parent, self.store.runs_root)


if __name__ == "__main__":
    unittest.main()
