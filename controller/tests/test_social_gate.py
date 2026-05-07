from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.extensions import register_all_routers
from app.startup.extensions import _disable_extracted_social_state


class ExperimentalSocialGateTests(unittest.TestCase):
    def test_social_routes_are_not_registered_by_default(self) -> None:
        app = FastAPI()
        app.state.settings = SimpleNamespace()
        register_all_routers(app)

        response = TestClient(app).post(
            "/social/empire/research",
            json={"niche": "browser tools", "subreddits": [], "yt_results": 1},
        )

        self.assertEqual(response.status_code, 404)

    def test_social_routes_stay_absent_when_legacy_flag_is_present(self) -> None:
        app = FastAPI()
        app.state.settings = SimpleNamespace(legacy_social_flag=True)
        app.state.viral_engine = None
        register_all_routers(app)

        response = TestClient(app).post(
            "/social/empire/research",
            json={"niche": "browser tools", "subreddits": [], "yt_results": 1},
        )

        self.assertEqual(response.status_code, 404)

    def test_extracted_social_state_is_inert(self) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(),
            )
        )

        _disable_extracted_social_state(app)

        self.assertIsNone(app.state.youtube_client)
        self.assertIsNone(app.state.veo3_client)
        self.assertIsNone(app.state.viral_engine)


if __name__ == "__main__":
    unittest.main()
