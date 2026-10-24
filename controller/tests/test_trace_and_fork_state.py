"""Two copies of a session's secrets that outlived every scrub (NAV-01, NAV-02).

- Playwright tracing was on by default, so every session wrote `trace.zip` holding
  each request's headers (`Authorization` and `Cookie` included) and bodies, and
  snapshots with typed text, none of it scrubbed. Tracing is now opt-in.
- `fork_session` exported the source session's cookies to
  `AUTH_ROOT/<session>/fork_<id>.json` (plaintext without an encryption key) to
  hand them to the new session, and never deleted the file. It is now removed as
  soon as the new session has loaded it, or failed to.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, StateContext, build_manager

REPO = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / ".env.example"
COMPOSE = REPO / "docker-compose.yml"


class TracingIsOptInTests(unittest.TestCase):
    def test_settings_default_is_off(self) -> None:
        self.assertFalse(Settings().enable_tracing)

    def test_opting_in_still_works(self) -> None:
        self.assertTrue(Settings(ENABLE_TRACING=True).enable_tracing)

    @unittest.skipUnless(ENV_EXAMPLE.is_file(), ".env.example is not in the controller image")
    def test_env_example_ships_it_off(self) -> None:
        values = re.findall(r"^#?\s*ENABLE_TRACING=(\S+)", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
        self.assertEqual(values, ["false"])

    @unittest.skipUnless(COMPOSE.is_file(), "docker-compose.yml is not in the controller image")
    def test_compose_defaults_it_off(self) -> None:
        values = re.findall(r"ENABLE_TRACING:\s*\$\{ENABLE_TRACING:-([^}]*)\}", COMPOSE.read_text(encoding="utf-8"))
        self.assertEqual(values, ["false"])


class ForkStateFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager, self.new_context = build_manager(self.root)
        self.manager.settings.max_sessions = 5
        with acting_as(ALICE):
            self.source_id = (await self.manager.create_session(name="alice"))["id"]
        session = self.manager.sessions[self.source_id]
        session.context = StateContext(session.page)
        session.page.url = "https://example.com/"

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def fork_files(self) -> list[Path]:
        return sorted((self.root / "auth" / self.source_id).glob("fork_*"))

    async def test_the_forked_session_gets_the_cookies_and_no_copy_is_left(self) -> None:
        loaded: list[dict] = []
        make_context = self.new_context.side_effect

        def load(**kwargs):
            state = kwargs.get("storage_state")
            # Playwright reads a path when the context is made, as this does.
            loaded.append(json.loads(Path(state).read_text(encoding="utf-8")) if isinstance(state, str) else state)
            return make_context(**kwargs)

        self.new_context.side_effect = load
        with acting_as(ALICE):
            forked = await self.manager.fork_session(self.source_id)

        self.assertEqual(forked["forked_from"], self.source_id)
        self.assertEqual([c["value"] for c in loaded[-1]["cookies"]], ["alice-cookie"])
        self.assertEqual(self.fork_files(), [])

    async def test_no_copy_is_left_when_the_new_session_fails(self) -> None:
        seen: list[bool] = []

        async def failing_create(**kwargs):
            seen.append(Path(kwargs["storage_state_path"]).is_file())
            raise RuntimeError("no browser for you")

        self.manager.create_session = AsyncMock(side_effect=failing_create)  # type: ignore[method-assign]
        with acting_as(ALICE), self.assertRaises(RuntimeError):
            await self.manager.fork_session(self.source_id)

        self.assertEqual(seen, [True], "the state must exist while the new session loads it")
        self.assertEqual(self.fork_files(), [])
