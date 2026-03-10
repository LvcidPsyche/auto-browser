from __future__ import annotations

import unittest

from app.metrics import MetricsRecorder


class MetricsRecorderTests(unittest.TestCase):
    def test_render_contains_expected_metrics(self) -> None:
        recorder = MetricsRecorder()
        recorder.record_http_request(method="GET", path="/healthz", status_code=200, duration_seconds=0.01)
        recorder.set_active_sessions(2)

        payload, content_type = recorder.render()
        text = payload.decode("utf-8")

        self.assertIn("auto_browser_http_requests_total", text)
        self.assertIn("auto_browser_http_request_duration_seconds", text)
        self.assertIn("auto_browser_active_sessions", text)
        self.assertEqual(content_type, "text/plain; version=0.0.4; charset=utf-8")

    def test_disabled_recorder_suppresses_output(self) -> None:
        recorder = MetricsRecorder(enabled=False)
        recorder.record_http_request(method="GET", path="/healthz", status_code=200, duration_seconds=0.01)
        recorder.set_active_sessions(2)

        payload, content_type = recorder.render()

        self.assertEqual(payload, b"")
        self.assertEqual(content_type, "text/plain; version=0.0.4; charset=utf-8")


if __name__ == "__main__":
    unittest.main()
