from __future__ import annotations

import math
import os
import unittest
from unittest.mock import patch

from app.config import Settings


class NonFiniteFloatTests(unittest.TestCase):
    def test_every_float_setting_refuses_nan_and_infinity(self):
        float_fields = {
            name: field.alias
            for name, field in Settings.model_fields.items()
            if isinstance(field.alias, str) and field.annotation is float
        }
        self.assertIn("artifact_retention_hours", float_fields)
        for name, alias in float_fields.items():
            for raw in ("nan", "inf", "-inf"):
                with self.subTest(field=name, raw=raw):
                    with patch.dict(os.environ, {alias: raw}):
                        with self.assertRaises(ValueError):
                            Settings(_env_file=None)

    def test_finite_and_non_positive_retention_still_load(self):
        for raw, expected in (("0", 0.0), ("-1", -1.0), ("12.5", 12.5)):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"ARTIFACT_RETENTION_HOURS": raw}):
                    value = Settings(_env_file=None).artifact_retention_hours
                self.assertEqual(value, expected)
                self.assertTrue(math.isfinite(value))
