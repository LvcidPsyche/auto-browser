"""Response hardening headers, and the sandbox on browser-produced artifacts.

/artifacts serves downloads whose file name and bytes a visited site chooses.
Served from the controller's origin, a downloaded .html ran script there; on a
tokenless loopback controller that script could call every route, including
auth-profile export. The sandbox CSP gives such a document an opaque origin and
no script at all.
"""

from __future__ import annotations

import shutil
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service, method_name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.client = self.stack.enter_context(TestClient(main_module.app))

        self.session_dir = Path(main_module.settings.artifact_root) / f"headers-{uuid4().hex[:8]}"
        (self.session_dir / "downloads").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.session_dir, True)

    def tearDown(self) -> None:
        self.stack.close()

    def test_a_downloaded_html_file_is_served_sandboxed(self) -> None:
        page = self.session_dir / "downloads" / "invoice.html"
        page.write_text("<script>fetch('/auth-profiles')</script>", encoding="utf-8")

        response = self.client.get(f"/artifacts/{self.session_dir.name}/downloads/invoice.html")

        self.assertEqual(response.status_code, 200)
        csp = response.headers["content-security-policy"]
        self.assertTrue(csp.startswith("sandbox"), csp)
        self.assertNotIn("allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_api_responses_carry_the_baseline_headers(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        # The artifact sandbox is scoped to /artifacts; the dashboard and share
        # pages depend on their own inline script.
        self.assertNotIn("content-security-policy", response.headers)

    def test_short_circuited_errors_are_covered_too(self) -> None:
        response = self.client.get("/sessions", headers={"host": "attacker.example"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
