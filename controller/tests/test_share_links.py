"""Share links: what the audit log says of a mint and of a link's first view."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
from app import session_share
from app.models import McpToolCallRequest
from app.routes.share import create_share_router
from app.session_share import SHARE_MINTED_EVENT, SHARE_VIEWED_EVENT, SessionShareManager, token_digest
from app.tool_gateway import McpToolGateway

SESSION_ID = "session-1"


class _Audit:
    """The audit log's append, kept in a list: each event as the keyword arguments it was written with."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    async def append(self, **event: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.events.append(event)

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["event_type"] == event_type]


def _manager(frame: Path) -> SimpleNamespace:
    async def get_session(session_id: str) -> SimpleNamespace:
        if session_id != SESSION_ID:
            raise KeyError(session_id)
        return SimpleNamespace(id=SESSION_ID)

    snapshot = {"url": "https://example.com/", "title": "Example", "screenshot_path": str(frame)}
    return SimpleNamespace(
        get_session=AsyncMock(side_effect=get_session),
        share_snapshot=AsyncMock(return_value=snapshot),
        audit=_Audit(),
        settings=SimpleNamespace(force_governed_approvals=False),
        ensure_session_accessible=AsyncMock(),
    )


class _Links(unittest.TestCase):
    """A share router over a fake manager and a real token manager, with one session and one frame of it on disk."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        artifact_root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (artifact_root / SESSION_ID).mkdir()
        self.frame = artifact_root / SESSION_ID / "20260101T000000000000Z-share-frame.png"
        self.frame.write_bytes(b"png")
        self.manager = _manager(self.frame)
        self.audit: _Audit = self.manager.audit
        self.share_manager = SessionShareManager(secret="a-secret-for-tests", ttl_minutes=10)
        api = FastAPI()
        api.include_router(
            create_share_router(
                manager=self.manager,
                share_manager=self.share_manager,
                settings=SimpleNamespace(artifact_root=str(artifact_root)),
            )
        )
        self.client = self.stack.enter_context(TestClient(api, raise_server_exceptions=False))

    def mint(self, session_id: str = SESSION_ID) -> str:
        response = self.client.post(f"/sessions/{session_id}/share", json={"ttl_minutes": 5})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def assert_names_no_token(self, event: dict[str, Any], token: str) -> None:
        written = json.dumps(event, default=str)
        payload, signature = token.split(".")
        for secret in (token, payload, signature):
            self.assertNotIn(secret, written)


class MintAuditTests(_Links):
    def test_a_mint_over_rest_is_audited_by_the_tokens_digest_and_never_the_token(self) -> None:
        response = self.client.post(f"/sessions/{SESSION_ID}/share", json={"ttl_minutes": 5})

        self.assertEqual(response.status_code, 200, response.text)
        minted = response.json()
        (event,) = self.audit.of(SHARE_MINTED_EVENT)
        self.assertEqual((event["status"], event["action"], event["session_id"]), ("ok", "share_session", SESSION_ID))
        self.assertEqual(
            event["details"],
            {
                "token_sha256": hashlib.sha256(minted["token"].encode()).hexdigest(),
                "scope": "observe",
                "expires_at": minted["expires_at"],
            },
        )
        self.assert_names_no_token(event, minted["token"])

    def test_a_mint_that_is_refused_writes_no_event(self) -> None:
        self.assertEqual(self.client.post("/sessions/unknown/share").status_code, 404)
        with patch.object(self.share_manager, "create_token", side_effect=ValueError("ttl_seconds must be positive")):
            self.assertEqual(self.client.post(f"/sessions/{SESSION_ID}/share").status_code, 400)

        self.assertEqual(self.audit.events, [])

    def test_a_mint_whose_event_cannot_be_written_hands_out_no_token(self) -> None:
        self.audit.fail_with = OSError(28, "No space left on device")

        response = self.client.post(f"/sessions/{SESSION_ID}/share")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("token", response.text)


class McpMintAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_mint_over_mcp_writes_the_same_event(self) -> None:
        manager = _manager(Path("unused.png"))
        gateway = McpToolGateway(
            manager=manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
            share_manager=SessionShareManager(secret="a-secret-for-tests", ttl_minutes=10),
        )

        response = await gateway.call_tool(
            McpToolCallRequest(name="browser.share_session", arguments={"session_id": SESSION_ID, "ttl_minutes": 5})
        )

        self.assertFalse(response.isError, response.content)
        minted = response.structuredContent
        (event,) = manager.audit.of(SHARE_MINTED_EVENT)
        self.assertEqual((event["status"], event["action"], event["session_id"]), ("ok", "share_session", SESSION_ID))
        self.assertEqual(
            event["details"],
            {"token_sha256": token_digest(minted["token"]), "scope": "observe", "expires_at": minted["expires_at"]},
        )

    async def test_a_mint_over_mcp_whose_event_cannot_be_written_answers_an_error_without_the_token(self) -> None:
        manager = _manager(Path("unused.png"))
        manager.audit.fail_with = OSError(28, "No space left on device")
        gateway = McpToolGateway(
            manager=manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
            share_manager=SessionShareManager(secret="a-secret-for-tests", ttl_minutes=10),
        )

        with self.assertLogs("app.tool_gateway.gateway", level="ERROR"):
            response = await gateway.call_tool(
                McpToolCallRequest(name="browser.share_session", arguments={"session_id": SESSION_ID})
            )

        self.assertTrue(response.isError)
        self.assertNotIn("token", json.dumps(response.model_dump(), default=str).replace("token_", ""))


class FirstViewAuditTests(_Links):
    def view_every_way(self, token: str) -> list[int]:
        return [
            self.client.get(f"/share/{token}").status_code,
            self.client.get(f"/share/{token}/observe").status_code,
            self.client.get(f"/share/{token}/screenshots/{self.frame.name}").status_code,
        ]

    def test_the_first_valid_view_of_a_token_is_audited_once_by_its_digest(self) -> None:
        token = self.mint()

        self.assertEqual(self.view_every_way(token) + self.view_every_way(token), [200] * 6)

        (event,) = self.audit.of(SHARE_VIEWED_EVENT)
        self.assertEqual((event["status"], event["action"], event["session_id"]), ("ok", "view_share", SESSION_ID))
        self.assertEqual(event["details"], {"token_sha256": token_digest(token)})
        (minted,) = self.audit.of(SHARE_MINTED_EVENT)
        self.assertEqual(event["details"]["token_sha256"], minted["details"]["token_sha256"])
        self.assert_names_no_token(event, token)

    def test_each_way_of_viewing_is_a_first_view(self) -> None:
        for way in ("", "/observe", f"/screenshots/{self.frame.name}"):
            with self.subTest(way=way):
                token = self.share_manager.create_token(SESSION_ID, ttl_seconds=60 + len(way))["token"]
                self.audit.events.clear()

                self.assertEqual(self.client.get(f"/share/{token}{way}").status_code, 200)

                self.assertEqual(
                    [event["details"] for event in self.audit.events], [{"token_sha256": token_digest(token)}]
                )

    def test_each_token_has_its_own_first_view(self) -> None:
        first = self.share_manager.create_token(SESSION_ID, ttl_seconds=60)["token"]
        second = self.share_manager.create_token(SESSION_ID, ttl_seconds=120)["token"]
        self.assertNotEqual(first, second)

        for token in (first, second, first, second):
            self.assertEqual(self.client.get(f"/share/{token}/observe").status_code, 200)

        self.assertEqual(
            [event["details"]["token_sha256"] for event in self.audit.of(SHARE_VIEWED_EVENT)],
            [token_digest(first), token_digest(second)],
        )

    def test_a_token_that_is_refused_or_shows_nothing_is_no_view(self) -> None:
        token = self.share_manager.create_token(SESSION_ID, ttl_seconds=60)["token"]
        gone = self.share_manager.create_token("closed-session", ttl_seconds=60)["token"]
        tampered = token[:-1] + ("1" if token.endswith("0") else "0")
        self.manager.share_snapshot.side_effect = self.manager.get_session  # a session that has gone: KeyError

        self.assertEqual(self.client.get(f"/share/{tampered}").status_code, 403)
        self.assertEqual(self.client.get(f"/share/{tampered}/observe").status_code, 403)
        self.assertEqual(self.client.get(f"/share/{gone}").status_code, 404)
        self.assertEqual(self.client.get(f"/share/{gone}/observe").status_code, 404)
        self.assertEqual(self.client.get(f"/share/{token}/screenshots/no-such-frame.png").status_code, 404)
        self.manager.share_snapshot.side_effect = PermissionError("the page is on a refused host")
        self.assertEqual(self.client.get(f"/share/{token}/observe").status_code, 403)

        self.assertEqual(self.audit.events, [])

    def test_the_tokens_remembered_are_bounded_and_a_forgotten_one_is_audited_again(self) -> None:
        tokens = [self.share_manager.create_token(SESSION_ID, ttl_seconds=60 + n)["token"] for n in range(3)]
        with patch.object(session_share, "SHARE_VIEWED_TOKENS_KEPT", 2):
            for token in tokens:
                self.client.get(f"/share/{token}/observe")
            self.assertEqual(len(self.audit.events), 3)
            self.assertEqual(len(self.share_manager._viewed), 2)

            self.client.get(f"/share/{tokens[2]}/observe")  # still remembered
            self.assertEqual(len(self.audit.events), 3)
            self.client.get(f"/share/{tokens[0]}/observe")  # forgotten when the third came

        self.assertEqual(
            [event["details"]["token_sha256"] for event in self.audit.events],
            [token_digest(token) for token in (*tokens, tokens[0])],
        )
        self.assertEqual(len(self.share_manager._viewed), 2)

    def test_a_token_in_use_is_not_the_one_forgotten(self) -> None:
        watched, *others = [self.share_manager.create_token(SESSION_ID, ttl_seconds=60 + n)["token"] for n in range(3)]
        with patch.object(session_share, "SHARE_VIEWED_TOKENS_KEPT", 2):
            self.client.get(f"/share/{watched}/observe")
            for token in others:
                self.client.get(f"/share/{token}/observe")
                self.client.get(f"/share/{watched}/observe")  # its page polls on

        self.assertEqual(
            [event["details"]["token_sha256"] for event in self.audit.events],
            [token_digest(token) for token in (watched, *others)],
        )

    def test_a_view_whose_event_cannot_be_written_is_refused_and_audited_when_it_can_be(self) -> None:
        token = self.mint()
        self.audit.fail_with = OSError(28, "No space left on device")

        with self.assertLogs("app.routes.share", level="ERROR"):
            statuses = self.view_every_way(token)
        self.assertEqual(statuses, [500, 500, 500])
        self.assertEqual(self.audit.of(SHARE_VIEWED_EVENT), [])

        self.audit.fail_with = None
        self.assertEqual(self.view_every_way(token), [200, 200, 200])
        self.assertEqual(len(self.audit.of(SHARE_VIEWED_EVENT)), 1)


class ConcurrentFirstViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_views_that_arrive_together_write_one_event(self) -> None:
        share_manager = SessionShareManager(secret="a-secret-for-tests", ttl_minutes=10)
        token = share_manager.create_token(SESSION_ID)["token"]
        writing, release = asyncio.Event(), asyncio.Event()
        events: list[dict[str, Any]] = []

        async def append(**event: Any) -> None:
            writing.set()
            await release.wait()
            events.append(event)

        audit = SimpleNamespace(append=append)
        views = [asyncio.ensure_future(share_manager.record_view(audit, token, SESSION_ID)) for _ in range(5)]
        await asyncio.wait_for(writing.wait(), timeout=5)
        self.assertFalse(any(view.done() for view in views))  # none is answered before the event is written
        release.set()
        await asyncio.wait_for(asyncio.gather(*views), timeout=5)

        self.assertEqual(len(events), 1)


class ViewerIdentityTests(unittest.TestCase):
    """Through the real app: a viewer holds a share token and no credential, so no header of its names the event."""

    API_TOKEN = "t" * 40

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service in (main_module.manager, main_module.job_queue, main_module.cron_service, main_module.maintenance):
            for method_name in ("startup", "shutdown"):
                self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.append = self.stack.enter_context(patch.object(main_module.manager.audit, "append", new=AsyncMock()))
        self.stack.enter_context(
            patch.object(main_module.manager, "get_session", new=AsyncMock(return_value=SimpleNamespace(id="x")))
        )
        self.client = self.stack.enter_context(TestClient(main_module.app))

    def view(self, **settings: Any) -> dict[str, Any]:
        # A session id of its own: the app's token manager remembers the tokens it has seen for the whole run.
        token = main_module.share_manager.create_token(f"share-{os.urandom(6).hex()}", ttl_seconds=60)["token"]
        with ExitStack() as stack:
            for name, value in settings.items():
                stack.enter_context(patch.object(main_module.settings, name, value))
            response = self.client.get(
                f"/share/{token}",
                headers={"X-Operator-Id": "alice", "X-Operator-Name": "Alice of the audit log", **self.headers},
            )
        self.assertEqual(response.status_code, 200, response.text)
        (call,) = [call for call in self.append.await_args_list if call.kwargs["event_type"] == SHARE_VIEWED_EVENT]
        self.append.reset_mock()
        return call.kwargs

    def test_a_viewers_own_operator_header_is_not_the_events_operator(self) -> None:
        self.headers: dict[str, str] = {}

        for settings in ({}, {"api_bearer_token": self.API_TOKEN}):
            with self.subTest(settings=settings):
                operator = self.view(**settings)["operator"]

                self.assertEqual((operator.id, operator.source, operator.name), ("anonymous", "anonymous", None))

    def test_a_viewer_who_proves_a_named_credential_is_the_events_operator(self) -> None:
        self.headers = {"Authorization": "Bearer " + "b" * 40}

        operator = self.view(api_bearer_tokens="bob:" + "b" * 40)["operator"]

        self.assertEqual((operator.id, operator.source), ("bob", "token"))


if __name__ == "__main__":
    unittest.main()
