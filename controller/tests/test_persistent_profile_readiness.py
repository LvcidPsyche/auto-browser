"""In persistent-profile mode there is no longer one shared browser for
/readyz and /healthz/deep to round-trip through -- each named profile's
Chromium launches lazily, on the first session that asks for it. Readiness
there has to mean "browser-node's control API answers" instead, or these
probes would report unhealthy forever the moment PERSISTENT_PROFILES_ENABLED
is turned on, even with everything working.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.system import create_system_router, run_deep_health_probe


def _fake_settings(*, persistent: bool) -> SimpleNamespace:
    return SimpleNamespace(
        persistent_profiles_enabled=persistent,
        session_isolation_mode="shared_browser_node",
        environment_name="test",
        cleanup_on_startup=False,
        cleanup_interval_seconds=0,
        artifact_retention_hours=0,
        upload_retention_hours=0,
        auth_retention_hours=0,
    )


def _fake_manager(*, persistent: bool, ping: AsyncMock, ensure_browser: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        settings=_fake_settings(persistent=persistent),
        persistent_profiles=SimpleNamespace(ping=ping),
        ensure_browser=ensure_browser,
        sessions={},
    )


class DeepHealthPersistentModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_mode_pings_browser_node_instead_of_opening_a_context(self) -> None:
        ping = AsyncMock()
        ensure_browser = AsyncMock(side_effect=AssertionError("must not touch the legacy shared browser"))
        manager = _fake_manager(persistent=True, ping=ping, ensure_browser=ensure_browser)

        result = await run_deep_health_probe(manager)

        ping.assert_awaited_once()
        ensure_browser.assert_not_awaited()
        self.assertEqual(result["status"], "ok")
        self.assertIn(
            "persistent_profile_control_api",
            [check["name"] for check in result["checks"]],
        )

    async def test_persistent_mode_ping_failure_propagates(self) -> None:
        ping = AsyncMock(side_effect=RuntimeError("browser-node unreachable"))
        ensure_browser = AsyncMock()
        manager = _fake_manager(persistent=True, ping=ping, ensure_browser=ensure_browser)

        with self.assertRaises(RuntimeError):
            await run_deep_health_probe(manager)


def _build_readyz_app(*, persistent: bool, ping: AsyncMock, ensure_browser: AsyncMock) -> FastAPI:
    app = FastAPI()
    settings = _fake_settings(persistent=persistent)
    manager = _fake_manager(persistent=persistent, ping=ping, ensure_browser=ensure_browser)
    router = create_system_router(
        settings=settings,
        manager=manager,
        metrics=SimpleNamespace(enabled=False),
        maintenance=SimpleNamespace(last_report=None),
        orchestrator=SimpleNamespace(list_providers=lambda: []),
        version="test",
    )
    app.include_router(router)
    return app


class ReadyzPersistentModeTests(unittest.TestCase):
    def test_readyz_uses_the_control_api_ping_in_persistent_mode(self) -> None:
        ping = AsyncMock()
        ensure_browser = AsyncMock(side_effect=AssertionError("must not touch the legacy shared browser"))
        app = _build_readyz_app(persistent=True, ping=ping, ensure_browser=ensure_browser)

        with TestClient(app) as client:
            response = client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        ping.assert_awaited_once()
        ensure_browser.assert_not_called()

    def test_readyz_reports_503_when_browser_node_is_unreachable(self) -> None:
        ping = AsyncMock(side_effect=RuntimeError("connection refused"))
        ensure_browser = AsyncMock()
        app = _build_readyz_app(persistent=True, ping=ping, ensure_browser=ensure_browser)

        with TestClient(app) as client:
            response = client.get("/readyz")

        self.assertEqual(response.status_code, 503)

    def test_readyz_still_uses_ensure_browser_when_persistent_profiles_are_off(self) -> None:
        ping = AsyncMock(side_effect=AssertionError("must not ping browser-node in legacy mode"))
        ensure_browser = AsyncMock()
        app = _build_readyz_app(persistent=False, ping=ping, ensure_browser=ensure_browser)

        with TestClient(app) as client:
            response = client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        ensure_browser.assert_awaited_once()
        ping.assert_not_called()


if __name__ == "__main__":
    unittest.main()
