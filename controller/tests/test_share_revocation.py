"""Share links: a revoke ends the links of its session, and what the store of revocations does when it cannot be used."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
from app import session_share
from app.browser_manager import BrowserSession
from app.models import McpToolCallRequest, ShareSessionRequest
from app.routes.share import create_share_router
from app.session_share import (
    SHARE_REVOCATIONS_UNAVAILABLE,
    SHARE_REVOKED_EVENT,
    SHARE_TOKEN_MAX_TTL_SECONDS,
    SHARE_VIEWED_EVENT,
    SessionShareManager,
    ShareRevocationStoreError,
)
from app.tool_gateway import McpToolGateway
from app.tool_inputs import ShareSessionInput
from app.utils import PRIVATE_FILE_MODE, UTC
from tests.test_browser_manager_create_session import FakePage
from tests.test_session_ownership import ALICE, BOB
from tests.test_share_links import CLOSED_SESSION_ID, SECRET, SESSION_ID, _Audit, _Links, _manager

REVOKE_TOOL = "browser.revoke_session_share"
LOGGER = "app.session_share"


class _Clock:
    """What session_share reads the time from, under a test's hand."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def _payload(token: str) -> dict[str, Any]:
    body = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _signed(manager: SessionShareManager, *, now: float | None = None, **fields: Any) -> str:
    """A token under the manager's key that says of its mint what ``fields`` say: nothing, as every token did before
    tokens carried an ``iat``, or an ``iat`` no mint writes."""
    payload = {"session_id": SESSION_ID, "exp": int(time.time() if now is None else now) + 600, "scope": "observe"}
    body = base64.urlsafe_b64encode(json.dumps({**payload, **fields}, separators=(",", ":")).encode()).decode()
    body = body.rstrip("=")
    return f"{body}.{manager._sign(body)}"


class _HeldAtRelease:
    """A manager's ``_store_lock`` whose first release by the ``minting`` thread waits until ``revoked`` is set: a
    revoke takes the lock after a mint has let it go, and before the mint goes on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.minting: threading.Thread | None = None
        self.released, self.revoked = threading.Event(), threading.Event()

    def __enter__(self) -> None:
        self._lock.acquire()

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()
        if threading.current_thread() is self.minting and not self.released.is_set():
            self.released.set()
            self.revoked.wait(5)


class _Stores(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.path = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "share" / "revocations.json"

    def manager(self, path: Path | None = None, *, ttl_minutes: int = 10) -> SessionShareManager:
        return SessionShareManager(secret=SECRET, ttl_minutes=ttl_minutes, revocations_path=path)

    def stored(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def clock(self) -> _Clock:
        clock = _Clock()
        self.stack.enter_context(patch.object(session_share, "time", clock))
        return clock


class RevokeTests(_Stores):
    def test_a_new_token_says_when_it_was_minted_in_milliseconds(self) -> None:
        manager = self.manager()
        before = int(time.time() * 1000)

        iat = _payload(manager.create_token(SESSION_ID)["token"])["iat"]

        self.assertIs(type(iat), int)
        self.assertTrue(before <= iat <= int(time.time() * 1000), iat)

    def test_a_revoke_ends_the_links_of_its_session_and_of_no_other(self) -> None:
        manager = self.manager()
        ended = [manager.create_token(SESSION_ID)["token"] for _ in range(2)]
        other = manager.create_token("session-2")["token"]

        revoked_before = manager.revoke(SESSION_ID)

        for token in ended:
            with self.assertRaisesRegex(ValueError, "revoked"):
                manager.validate_token(token)
            self.assertEqual(manager.token_info(token), {"valid": False, "error": "Invalid token"})
            self.assertLessEqual(_payload(token)["iat"], revoked_before)
        self.assertTrue(manager.token_info(other)["valid"])

    def test_a_link_minted_after_a_revoke_works_though_the_clock_has_not_moved(self) -> None:
        self.clock()
        manager = self.manager()
        old = manager.create_token(SESSION_ID)["token"]

        manager.revoke(SESSION_ID)
        new = manager.create_token(SESSION_ID)["token"]

        self.assertFalse(manager.token_info(old)["valid"])
        self.assertTrue(manager.token_info(new)["valid"])
        manager.revoke(SESSION_ID)
        self.assertFalse(manager.token_info(new)["valid"])

    def test_a_token_that_does_not_say_when_it_was_minted_is_good_until_its_session_is_revoked(self) -> None:
        manager = self.manager()
        legacy = _signed(manager)

        self.assertTrue(manager.token_info(legacy)["valid"])
        manager.revoke("session-2")
        self.assertTrue(manager.token_info(legacy)["valid"])
        manager.revoke(SESSION_ID)
        with self.assertRaisesRegex(ValueError, "revoked"):
            manager.validate_token(legacy)

    def test_an_iat_no_mint_writes_counts_as_none_however_late_it_reads(self) -> None:
        for iat in (9e18, "9999999999999999999", True, None, -1, [9_999_999_999_999_999]):
            with self.subTest(iat=iat):
                manager = self.manager()
                token = _signed(manager, iat=iat)

                self.assertTrue(manager.token_info(token)["valid"])
                manager.revoke(SESSION_ID)
                self.assertFalse(manager.token_info(token)["valid"])

    def test_without_a_store_a_revoke_holds_until_the_restart_and_writes_nothing(self) -> None:
        manager = self.manager()
        token = manager.create_token(SESSION_ID)["token"]

        manager.revoke(SESSION_ID)

        self.assertFalse(manager.token_info(token)["valid"])
        self.assertFalse(self.path.parent.exists())

    def test_no_link_outlives_the_life_a_revocation_is_kept_for(self) -> None:
        manager = self.manager()
        manager.create_token(SESSION_ID, ttl_seconds=SHARE_TOKEN_MAX_TTL_SECONDS)
        with self.assertRaisesRegex(ValueError, "ttl_seconds"):
            manager.create_token(SESSION_ID, ttl_seconds=SHARE_TOKEN_MAX_TTL_SECONDS + 1)
        # What a route or a tool is asked for stops at the same life.
        longest_minutes = SHARE_TOKEN_MAX_TTL_SECONDS // 60
        for model in (ShareSessionRequest, ShareSessionInput):
            with self.subTest(model=model.__name__):
                self.assertEqual(model(ttl_minutes=longest_minutes).ttl_minutes, longest_minutes)
                with self.assertRaises(ValidationError):
                    model(ttl_minutes=longest_minutes + 1)
        # A deployment whose own default is longer mints up to that, and no further.
        two_days = 2 * SHARE_TOKEN_MAX_TTL_SECONDS
        longer = self.manager(ttl_minutes=two_days // 60)
        self.assertEqual(longer.create_token(SESSION_ID)["expires_in_seconds"], two_days)
        with self.assertRaisesRegex(ValueError, "ttl_seconds"):
            longer.create_token(SESSION_ID, ttl_seconds=two_days + 1)


class RevocationStoreTests(_Stores):
    def test_a_revocation_outlives_a_restart(self) -> None:
        first = self.manager(self.path)
        old = first.create_token(SESSION_ID)["token"]
        revoked_before = first.revoke(SESSION_ID)
        kept = first.create_token(SESSION_ID)["token"]

        # The last mint's stamp is kept too, written before its link was handed out.
        self.assertEqual(
            self.stored(), {"all": 0, "sessions": {SESSION_ID: revoked_before}, "minted": _payload(kept)["iat"]}
        )
        restarted = self.manager(self.path)
        self.assertFalse(restarted.token_info(old)["valid"])
        self.assertTrue(restarted.token_info(kept)["valid"])

    def test_after_a_restart_with_the_clock_set_back_a_new_link_is_not_one_a_stored_revocation_ends(self) -> None:
        clock = self.clock()
        self.manager(self.path).revoke(SESSION_ID)
        clock.now -= 3600

        restarted = self.manager(self.path)

        self.assertTrue(restarted.token_info(restarted.create_token(SESSION_ID)["token"])["valid"])

    def test_after_a_restart_with_the_clock_set_back_a_revoke_ends_a_link_minted_before_it(self) -> None:
        clock = self.clock()
        token = self.manager(self.path).create_token(SESSION_ID)["token"]
        # The restart: the host's clock had run ten minutes fast, and is stepped back as the controller comes up.
        clock.now -= 600

        restarted = self.manager(self.path)
        revoked_before = restarted.revoke(SESSION_ID)

        self.assertGreater(revoked_before, _payload(token)["iat"])
        self.assertFalse(restarted.token_info(token)["valid"])
        self.assertTrue(restarted.token_info(restarted.create_token(SESSION_ID)["token"])["valid"])

    def test_a_mint_whose_stamp_cannot_be_saved_hands_out_no_link(self) -> None:
        manager = self.manager(self.path)

        with (
            patch.object(session_share, "atomic_write_text", side_effect=OSError(28, "No space left on device")),
            self.assertLogs(LOGGER, level="ERROR") as logs,
            self.assertRaises(ShareRevocationStoreError) as caught,
        ):
            manager.create_token(SESSION_ID)

        self.assertEqual(str(caught.exception), SHARE_REVOCATIONS_UNAVAILABLE)
        self.assertIn("No space left on device", logs.output[0])
        self.assertFalse(self.path.exists())
        token = manager.create_token(SESSION_ID)["token"]
        self.assertEqual(self.stored()["minted"], _payload(token)["iat"])

    def test_a_store_that_holds_no_mint_is_read_and_the_next_mint_adds_one(self) -> None:
        # What the first 1.18.0 builds wrote, before a mint's stamp was kept.
        clock = self.clock()
        revoked_before = int(clock.now * 1000) - 60_000
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"all": 0, "sessions": {SESSION_ID: revoked_before}}), encoding="utf-8")

        with self.assertNoLogs(LOGGER, level="ERROR"):
            manager = self.manager(self.path)

        self.assertFalse(manager.token_info(_signed(manager, now=clock.now))["valid"])
        token = manager.create_token(SESSION_ID)["token"]
        self.assertTrue(manager.token_info(token)["valid"])
        self.assertEqual(
            self.stored(), {"all": 0, "sessions": {SESSION_ID: revoked_before}, "minted": _payload(token)["iat"]}
        )

    def test_the_store_is_written_whole_and_private(self) -> None:
        with patch.object(session_share, "atomic_write_text", side_effect=session_share.atomic_write_text) as write:
            self.manager(self.path).revoke(SESSION_ID)

        (call,) = write.call_args_list
        self.assertEqual((call.args[0], call.kwargs), (self.path, {"mode": PRIVATE_FILE_MODE}))
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], [self.path.name])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_revocation_older_than_the_longest_token_life_is_dropped(self) -> None:
        clock = self.clock()
        manager = self.manager(self.path)
        manager.revoke("session-old")

        # The last second a link minted just before that revoke can be good in: the revocation is still kept.
        clock.now += SHARE_TOKEN_MAX_TTL_SECONDS + 1
        manager.revoke("session-kept")
        self.assertEqual(sorted(self.stored()["sessions"]), ["session-kept", "session-old"])
        clock.now += 1
        manager.revoke("session-new")
        self.assertEqual(sorted(self.stored()["sessions"]), ["session-kept", "session-new"])

    def test_a_link_revoked_as_it_is_minted_is_still_refused_once_its_revocation_is_dropped(self) -> None:
        clock = self.clock()
        clock.now += 0.980
        manager = self.manager(self.path)
        lock = manager._store_lock = _HeldAtRelease()
        seen: dict[str, Any] = {}

        def mint() -> None:
            seen.update(manager.create_token(SESSION_ID, ttl_seconds=SHARE_TOKEN_MAX_TTL_SECONDS))

        def revoke() -> None:
            # Between the mint's stamp and the rest of the mint, which goes on in the next second.
            if lock.released.wait(5):
                clock.now += 0.015
                seen["revoked_before"] = manager.revoke(SESSION_ID)
                clock.now += 0.010
            lock.revoked.set()

        lock.minting = threading.Thread(target=mint)
        revoking = threading.Thread(target=revoke)
        for thread in (lock.minting, revoking):
            thread.start()
        for thread in (lock.minting, revoking):
            thread.join(10)
        token = seen["token"]
        self.assertFalse(manager.token_info(token)["valid"])

        # Past the last millisecond the revocation is kept for, a revoke of any session drops it.
        clock.now = seen["revoked_before"] / 1000 + SHARE_TOKEN_MAX_TTL_SECONDS + 1.002
        manager.revoke("session-2")

        self.assertNotIn(SESSION_ID, self.stored()["sessions"])
        self.assertFalse(manager.token_info(token)["valid"])

    def test_a_deployment_with_longer_links_keeps_its_revocations_as_long(self) -> None:
        clock = self.clock()
        manager = self.manager(self.path, ttl_minutes=2 * SHARE_TOKEN_MAX_TTL_SECONDS // 60)
        manager.revoke("session-old")

        clock.now += 2 * SHARE_TOKEN_MAX_TTL_SECONDS + 1
        manager.revoke("session-kept")
        self.assertEqual(sorted(self.stored()["sessions"]), ["session-kept", "session-old"])
        clock.now += 1
        manager.revoke("session-new")
        self.assertEqual(sorted(self.stored()["sessions"]), ["session-kept", "session-new"])

    def test_revokes_made_together_all_reach_the_store(self) -> None:
        manager = self.manager(self.path)
        sessions = [f"session-{number}" for number in range(6)]
        write = session_share.atomic_write_text

        def slowly(*args: Any, **kwargs: Any) -> None:
            # Long enough that, unserialized, every revoke has read the store before the first has written it.
            time.sleep(0.05)
            write(*args, **kwargs)

        with patch.object(session_share, "atomic_write_text", side_effect=slowly):
            threads = [threading.Thread(target=manager.revoke, args=(session_id,)) for session_id in sessions]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sorted(self.stored()["sessions"]), sessions)

    def test_a_revoke_that_cannot_be_saved_revokes_nothing(self) -> None:
        manager = self.manager(self.path)
        token = manager.create_token(SESSION_ID)["token"]
        stored = self.stored()

        with (
            patch.object(session_share, "atomic_write_text", side_effect=OSError(28, "No space left on device")),
            self.assertLogs(LOGGER, level="ERROR") as logs,
            self.assertRaises(ShareRevocationStoreError) as caught,
        ):
            manager.revoke(SESSION_ID)

        self.assertEqual(str(caught.exception), SHARE_REVOCATIONS_UNAVAILABLE)
        self.assertIn("No space left on device", logs.output[0])
        self.assertTrue(manager.token_info(token)["valid"])
        self.assertEqual(self.stored(), stored)
        manager.revoke(SESSION_ID)
        self.assertFalse(manager.token_info(token)["valid"])

    def test_while_the_store_cannot_be_read_no_link_is_honoured_minted_or_revoked(self) -> None:
        first = self.manager(self.path)
        old = first.create_token(SESSION_ID)["token"]
        first.revoke(SESSION_ID)
        kept = first.create_token(SESSION_ID)["token"]
        stored = self.path.read_text(encoding="utf-8")
        # A directory where the file was: reading it fails, and not as a file that is not there.
        self.path.unlink()
        self.path.mkdir()

        with self.assertLogs(LOGGER, level="ERROR") as logs:
            unreadable = self.manager(self.path)

        self.assertIn("left where it is", logs.output[0])
        # Every poll of every open link validates its token: none of them reads the store again or logs.
        with self.assertNoLogs(LOGGER, level="ERROR"):
            self.assertEqual([unreadable.token_info(token)["valid"] for token in (old, kept)], [False, False])
        for refused in (lambda: unreadable.create_token(SESSION_ID), lambda: unreadable.revoke(SESSION_ID)):
            with self.assertLogs(LOGGER, level="ERROR"), self.assertRaises(ShareRevocationStoreError) as caught:
                refused()
            self.assertEqual(str(caught.exception), SHARE_REVOCATIONS_UNAVAILABLE)
            self.assertNotIn(str(self.path.parent), str(caught.exception))
        self.assertTrue(self.path.is_dir())

        # Readable again: the next mint reads it, and what it revoked is still revoked.
        self.path.rmdir()
        self.path.write_text(stored, encoding="utf-8")
        new = unreadable.create_token(SESSION_ID)["token"]
        self.assertEqual([unreadable.token_info(token)["valid"] for token in (old, kept, new)], [False, True, True])

    def test_a_corrupt_store_ends_every_link_minted_before_it_was_found(self) -> None:
        corrupt = (
            b"not json",
            b"\xff\xfe",
            b"[]",
            b'{"all": 0, "sessions": []}',
            b'{"all": 0, "sessions": {"session-1": "soon"}}',
            b'{"all": 0, "sessions": {"session-1": true}}',
            b'{"all": -1, "sessions": {}}',
            b'{"all": 1.5, "sessions": {}}',
            b'{"all": 0, "sessions": {}, "minted": "soon"}',
            b'{"all": 0, "sessions": {}, "minted": true}',
            b'{"all": 0, "sessions": {}, "minted": -1}',
            b'{"all": 0, "sessions": {}, "minted": 1.5}',
            # Times, without a key every save writes or with one no save writes.
            b"{}",
            b'{"all": 0, "minted": 0}',
            b'{"sessions": {}, "minted": 0}',
            b'{"all": 0, "sessions": {}, "minted": 0, "revoked": {}}',
        )
        clock = self.clock()
        for number, content in enumerate(corrupt):
            with self.subTest(content=content):
                path = self.path.with_name(f"revocations-{number}.json")
                minter = self.manager()
                before = [minter.create_token(SESSION_ID)["token"], minter.create_token("session-2")["token"]]
                before.append(_signed(minter, now=clock.now))
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(content)
                # The controller that finds it started later: within one millisecond, a second mint's time is a
                # millisecond ahead of the clock the mark is read from.
                clock.now += 1

                with self.assertLogs(LOGGER, level="ERROR") as logs:
                    found = self.manager(path)

                self.assertIn("every link minted before now is revoked", logs.output[0])
                self.assertEqual([found.token_info(token)["valid"] for token in before], [False, False, False])
                after = found.create_token(SESSION_ID)["token"]
                self.assertTrue(found.token_info(after)["valid"])
                # The store is whole again and says so: a restart finds the same, and logs nothing.
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual((stored["sessions"], stored["all"] > 0), ({}, True))
                with self.assertNoLogs(LOGGER, level="ERROR"):
                    restarted = self.manager(path)
                self.assertEqual([restarted.token_info(token)["valid"] for token in before], [False, False, False])
                self.assertTrue(restarted.token_info(after)["valid"])

    def test_a_corrupt_store_that_cannot_be_written_again_is_one_that_cannot_be_read(self) -> None:
        minter = self.manager()
        token = minter.create_token(SESSION_ID)["token"]
        self.path.parent.mkdir()
        self.path.write_text("not json", encoding="utf-8")

        with (
            patch.object(session_share, "atomic_write_text", side_effect=OSError(30, "Read-only file system")),
            self.assertLogs(LOGGER, level="ERROR"),
        ):
            found = self.manager(self.path)
            self.assertFalse(found.token_info(token)["valid"])
            with self.assertRaises(ShareRevocationStoreError):
                found.create_token(SESSION_ID)

        self.assertEqual(self.path.read_text(encoding="utf-8"), "not json")

    def test_after_a_restart_with_the_clock_set_back_a_corrupt_store_ends_a_link_minted_before_it(self) -> None:
        clock = self.clock()
        token = self.manager(self.path).create_token(SESSION_ID)["token"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("not json", encoding="utf-8")
        # The restart: the clock is stepped back by as long as a link can live.
        clock.now -= SHARE_TOKEN_MAX_TTL_SECONDS

        with self.assertLogs(LOGGER, level="ERROR"):
            found = self.manager(self.path)

        self.assertFalse(found.token_info(token)["valid"])
        self.assertTrue(found.token_info(found.create_token(SESSION_ID)["token"])["valid"])

    def test_the_mark_a_corrupt_store_left_is_dropped_with_the_last_link_it_could_end(self) -> None:
        clock = self.clock()
        self.path.parent.mkdir()
        self.path.write_text("not json", encoding="utf-8")
        with self.assertLogs(LOGGER, level="ERROR"):
            manager = self.manager(self.path)

        # The mark is a link's longest life past the time the store was found corrupt, and is kept as long again.
        clock.now += 2 * (SHARE_TOKEN_MAX_TTL_SECONDS + 1)
        manager.revoke("session-kept")
        self.assertGreater(self.stored()["all"], 0)
        clock.now += 1
        manager.revoke("session-new")
        self.assertEqual(self.stored()["all"], 0)


class RevokeRouteTests(_Links):
    def revoke(self, session_id: str = SESSION_ID) -> Any:
        return self.client.delete(f"/sessions/{session_id}/share")

    def views(self, token: str) -> list[int]:
        return [
            self.client.get(f"/share/{token}").status_code,
            self.client.get(f"/share/{token}/observe").status_code,
            self.client.get(f"/share/{token}/screenshots/{self.frame.name}").status_code,
        ]

    def test_a_revoked_link_is_refused_every_way_and_one_minted_afterwards_works(self) -> None:
        token = self.mint()
        self.assertEqual(self.views(token), [200, 200, 200])

        response = self.revoke()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["session_id"], body["revoked"], type(body["revoked_before"])), (SESSION_ID, True, int))
        self.assertEqual(self.views(token), [403, 403, 403])
        self.assertEqual(self.views(self.mint()), [200, 200, 200])

    def test_a_revoke_is_audited(self) -> None:
        body = self.revoke().json()

        (event,) = self.audit.of(SHARE_REVOKED_EVENT)
        self.assertEqual(
            (event["status"], event["action"], event["session_id"]), ("ok", "revoke_session_share", SESSION_ID)
        )
        self.assertEqual(event["details"], {"revoked_before": body["revoked_before"]})

    def test_a_revoked_link_that_is_tried_is_no_view(self) -> None:
        token = self.mint()
        self.revoke()

        self.views(token)

        self.assertEqual(self.audit.of(SHARE_VIEWED_EVENT), [])

    def test_a_revoke_of_a_session_that_never_was_is_404_and_ends_nothing(self) -> None:
        token = self.mint()

        response = self.revoke("unknown")

        self.assertEqual((response.status_code, response.json()), (404, {"detail": "Unknown session"}))
        self.assertEqual(self.audit.of(SHARE_REVOKED_EVENT), [])
        self.assertEqual(self.views(token), [200, 200, 200])

    def test_a_closed_sessions_links_are_revoked_too(self) -> None:
        # A link of a closed session still opens the frames that were taken for it.
        token = self.share_manager.create_token(CLOSED_SESSION_ID)["token"]

        response = self.revoke(CLOSED_SESSION_ID)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.share_manager.token_info(token)["valid"])
        self.assertEqual([event["session_id"] for event in self.audit.of(SHARE_REVOKED_EVENT)], [CLOSED_SESSION_ID])

    def test_a_revoke_whose_event_cannot_be_written_fails_and_the_links_stay_ended(self) -> None:
        token = self.mint()
        self.audit.fail_with = OSError(28, "No space left on device")

        response = self.revoke()

        self.assertEqual(response.status_code, 500)
        self.audit.fail_with = None
        self.assertEqual(self.views(token), [403, 403, 403])


class _HeldViewAudit(_Audit):
    """The audit log, whose ``share_viewed`` waits for ``hold_view`` while a test sets one."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_view: asyncio.Event | None = None
        self.view_begun = asyncio.Event()

    async def append(self, **event: Any) -> None:
        if event["event_type"] == SHARE_VIEWED_EVENT and self.hold_view is not None:
            self.view_begun.set()
            await self.hold_view.wait()
        await super().append(**event)


class RevokeWhileAViewWaitsTests(unittest.IsolatedAsyncioTestCase):
    """A share route checks its token and then waits: a poll for the session's frame (for as long as the session's
    agent holds its lock), the page for the session's lookup, and a first view for its event. A link revoked or expired
    during a wait is refused all the same, and one refused before its event is no view. No sleeps: each wait is held
    open by the test, and the expiry moves a clock the test owns."""

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        artifact_root = Path(directory.name)
        (artifact_root / SESSION_ID).mkdir()
        self.frame = artifact_root / SESSION_ID / "20260101T000000000000Z-share-frame.png"
        self.frame.write_bytes(b"png")
        self.frame_asked, self.frame_ready = asyncio.Event(), asyncio.Event()
        self.lookup_held, self.lookup_asked, self.lookup_ready = False, asyncio.Event(), asyncio.Event()
        manager = _manager(self.frame)
        lookup = manager.get_session

        async def share_snapshot(session_id: str) -> dict[str, Any]:
            self.frame_asked.set()
            await self.frame_ready.wait()  # a capture queued behind the session's lock
            return {"url": "https://example.com/after", "title": "after", "screenshot_path": str(self.frame)}

        async def get_session(session_id: str) -> Any:
            if self.lookup_held:  # the page's lookup, and not the revoke's after it
                self.lookup_held = False
                self.lookup_asked.set()
                await self.lookup_ready.wait()
            return await lookup(session_id)

        manager.share_snapshot, manager.get_session = share_snapshot, get_session
        self.audit = manager.audit = _HeldViewAudit()
        self.share_manager = SessionShareManager(secret=SECRET, ttl_minutes=10)
        api = FastAPI()
        api.include_router(
            create_share_router(
                manager=manager,
                share_manager=self.share_manager,
                settings=SimpleNamespace(artifact_root=str(artifact_root)),
            )
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def mint(self, *, ttl_seconds: int = 300) -> str:
        return self.share_manager.create_token(SESSION_ID, ttl_seconds=ttl_seconds)["token"]

    async def waiting(self, path: str, asked: asyncio.Event) -> asyncio.Future[httpx.Response]:
        """The request to ``path``, started, once it waits where ``asked`` says."""
        request = asyncio.ensure_future(self.client.get(path))
        await asyncio.wait_for(asked.wait(), 5)
        return request

    async def revoke(self) -> None:
        response = await self.client.delete(f"/sessions/{SESSION_ID}/share")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_a_viewed_link_revoked_while_its_poll_waits_for_the_frame_is_refused(self) -> None:
        token = self.mint()
        self.frame_ready.set()
        self.assertEqual((await self.client.get(f"/share/{token}/observe")).status_code, 200)
        self.frame_ready.clear()
        self.frame_asked.clear()
        poll = await self.waiting(f"/share/{token}/observe", self.frame_asked)
        await self.revoke()

        self.frame_ready.set()

        self.assertEqual((await poll).status_code, 403)

    async def test_a_link_revoked_while_its_first_poll_waits_for_the_frame_is_refused_and_no_view(self) -> None:
        token = self.mint()
        poll = await self.waiting(f"/share/{token}/observe", self.frame_asked)
        await self.revoke()

        self.frame_ready.set()

        self.assertEqual((await poll).status_code, 403)
        self.assertEqual(self.audit.of(SHARE_VIEWED_EVENT), [])

    async def test_a_link_that_expires_while_its_poll_waits_for_the_frame_is_refused_and_no_view(self) -> None:
        clock = _Clock()
        with patch.object(session_share, "time", clock):
            token = self.mint(ttl_seconds=60)
            poll = await self.waiting(f"/share/{token}/observe", self.frame_asked)
            clock.now += 61

            self.frame_ready.set()

            self.assertEqual((await poll).status_code, 403)
        self.assertEqual(self.audit.of(SHARE_VIEWED_EVENT), [])

    async def test_a_link_revoked_while_its_first_poll_is_audited_is_refused_with_the_view_written(self) -> None:
        token = self.mint()
        self.frame_ready.set()
        self.audit.hold_view = asyncio.Event()
        poll = await self.waiting(f"/share/{token}/observe", self.audit.view_begun)
        await self.revoke()

        self.audit.hold_view.set()

        self.assertEqual((await poll).status_code, 403)
        self.assertEqual(len(self.audit.of(SHARE_VIEWED_EVENT)), 1)

    async def test_a_link_revoked_while_its_first_image_is_audited_is_refused(self) -> None:
        token = self.mint()
        self.audit.hold_view = asyncio.Event()
        image = await self.waiting(f"/share/{token}/screenshots/{self.frame.name}", self.audit.view_begun)
        await self.revoke()

        self.audit.hold_view.set()

        self.assertEqual((await image).status_code, 403)

    async def test_a_link_revoked_while_its_page_looks_the_session_up_is_refused_and_no_view(self) -> None:
        token = self.mint()
        self.lookup_held = True
        page = await self.waiting(f"/share/{token}", self.lookup_asked)
        await self.revoke()

        self.lookup_ready.set()

        self.assertEqual((await page).status_code, 403)
        self.assertEqual(self.audit.of(SHARE_VIEWED_EVENT), [])

    async def test_a_link_revoked_while_its_first_page_is_audited_is_refused(self) -> None:
        token = self.mint()
        self.audit.hold_view = asyncio.Event()
        page = await self.waiting(f"/share/{token}", self.audit.view_begun)
        await self.revoke()

        self.audit.hold_view.set()

        self.assertEqual((await page).status_code, 403)


class UnreadableStoreRouteTests(_Links):
    def new_share_manager(self) -> SessionShareManager:
        store = self.root / "share" / "revocations.json"
        store.mkdir(parents=True)
        with self.assertLogs(LOGGER, level="ERROR"):
            return SessionShareManager(secret=SECRET, ttl_minutes=10, revocations_path=store)

    def test_a_mint_and_a_revoke_answer_503_with_a_text_that_names_no_path(self) -> None:
        with self.assertLogs(LOGGER, level="ERROR"):
            answers = [
                self.client.post(f"/sessions/{SESSION_ID}/share"),
                self.client.delete(f"/sessions/{SESSION_ID}/share"),
            ]

        for response in answers:
            self.assertEqual((response.status_code, response.json()), (503, {"detail": SHARE_REVOCATIONS_UNAVAILABLE}))
            self.assertNotIn(self.root.name, response.text)
        self.assertEqual(self.audit.events, [])


class UnwritableStoreRouteTests(_Links):
    def new_share_manager(self) -> SessionShareManager:
        return SessionShareManager(
            secret=SECRET, ttl_minutes=10, revocations_path=self.root / "share" / "revocations.json"
        )

    def test_a_mint_whose_stamp_cannot_be_saved_answers_503_and_no_token(self) -> None:
        with (
            patch.object(session_share, "atomic_write_text", side_effect=OSError(28, "No space left on device")),
            self.assertLogs(LOGGER, level="ERROR"),
        ):
            response = self.client.post(f"/sessions/{SESSION_ID}/share")

        self.assertEqual((response.status_code, response.json()), (503, {"detail": SHARE_REVOCATIONS_UNAVAILABLE}))
        self.assertEqual(self.audit.events, [])
        self.assertEqual(self.client.get(f"/share/{self.mint()}/observe").status_code, 200)


class MintOffTheLoopTests(unittest.IsolatedAsyncioTestCase):
    """A mint reads and writes the store in a worker thread: a slow disk holds the mint, not every other request."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "share" / "revocations.json"

    async def assert_minted_while_the_loop_runs(self, manager: SessionShareManager, name: str) -> None:
        """Mint while ``manager.<name>`` waits until the event loop has run again: in vain, were it on the loop."""
        reached, loop_ran = threading.Event(), threading.Event()
        waited: list[bool] = []
        call = getattr(manager, name)

        def slowly(*args: Any, **kwargs: Any) -> Any:
            reached.set()
            waited.append(loop_ran.wait(timeout=5))
            return call(*args, **kwargs)

        with patch.object(manager, name, side_effect=slowly):
            minting = asyncio.ensure_future(
                session_share.mint_share_token(manager, _Audit(), SESSION_ID, ttl_seconds=60)
            )
            self.assertTrue(await asyncio.to_thread(reached.wait, 5))
            loop_ran.set()
            token = await minting

        self.assertEqual(waited, [True])
        self.assertTrue(manager.token_info(token["token"])["valid"])

    async def test_a_mint_reads_a_store_it_could_not_read_before_off_the_loop(self) -> None:
        self.path.mkdir(parents=True)
        with self.assertLogs(LOGGER, level="ERROR"):
            manager = SessionShareManager(secret=SECRET, ttl_minutes=10, revocations_path=self.path)
        self.path.rmdir()

        await self.assert_minted_while_the_loop_runs(manager, "_read_revocations")

    async def test_a_mint_writes_its_stamp_off_the_loop(self) -> None:
        manager = SessionShareManager(secret=SECRET, ttl_minutes=10, revocations_path=self.path)

        await self.assert_minted_while_the_loop_runs(manager, "_write_revocations")


class McpMintStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_mint_whose_stamp_cannot_be_saved_is_a_tool_error_with_its_fixed_text(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = _manager(Path("unused.png"))
        gateway = McpToolGateway(
            manager=manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
            share_manager=SessionShareManager(
                secret=SECRET, ttl_minutes=10, revocations_path=Path(directory.name) / "revocations.json"
            ),
        )

        with (
            patch.object(session_share, "atomic_write_text", side_effect=OSError(28, "No space left on device")),
            self.assertLogs(LOGGER, level="ERROR"),
            self.assertNoLogs("app.tool_gateway.gateway", level="ERROR"),
        ):
            response = await gateway.call_tool(
                McpToolCallRequest(name="browser.share_session", arguments={"session_id": SESSION_ID})
            )

        self.assertTrue(response.isError)
        answered = json.dumps(response.model_dump(), default=str)
        self.assertIn(SHARE_REVOCATIONS_UNAVAILABLE, answered)
        self.assertNotIn('"token"', answered)
        self.assertEqual(manager.audit.events, [])


class McpRevokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = _manager(Path("unused.png"))
        self.share_manager = SessionShareManager(secret=SECRET, ttl_minutes=10)
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
            share_manager=self.share_manager,
        )

    async def call(self, session_id: str) -> Any:
        return await self.gateway.call_tool(McpToolCallRequest(name=REVOKE_TOOL, arguments={"session_id": session_id}))

    async def test_the_tool_revokes_and_audits_as_the_route_does(self) -> None:
        tokens = [
            self.share_manager.create_token(session_id)["token"] for session_id in (SESSION_ID, CLOSED_SESSION_ID)
        ]

        answers = [await self.call(SESSION_ID), await self.call(CLOSED_SESSION_ID)]

        for response in answers:
            self.assertFalse(response.isError, response.content)
        self.assertEqual([self.share_manager.token_info(token)["valid"] for token in tokens], [False, False])
        first = answers[0].structuredContent
        self.assertEqual((first["session_id"], first["revoked"]), (SESSION_ID, True))
        events = self.manager.audit.of(SHARE_REVOKED_EVENT)
        self.assertEqual([event["session_id"] for event in events], [SESSION_ID, CLOSED_SESSION_ID])
        self.assertEqual(
            (events[0]["status"], events[0]["action"], events[0]["details"]),
            ("ok", "revoke_session_share", {"revoked_before": first["revoked_before"]}),
        )

    async def test_a_session_that_never_was_is_a_tool_error_that_ends_nothing(self) -> None:
        token = self.share_manager.create_token(SESSION_ID)["token"]

        response = await self.call("unknown")

        self.assertTrue(response.isError)
        self.assertEqual(self.manager.audit.events, [])
        self.assertTrue(self.share_manager.token_info(token)["valid"])

    async def test_a_store_that_cannot_be_used_is_a_tool_error_with_its_fixed_text(self) -> None:
        with (
            patch.object(self.share_manager, "revoke", side_effect=ShareRevocationStoreError),
            self.assertNoLogs("app.tool_gateway.gateway", level="ERROR"),
        ):
            response = await self.call(SESSION_ID)

        self.assertTrue(response.isError)
        self.assertIn(SHARE_REVOCATIONS_UNAVAILABLE, json.dumps(response.model_dump(), default=str))
        self.assertEqual(self.manager.audit.events, [])

    async def test_the_tool_is_listed_after_the_mint_and_says_it_destroys(self) -> None:
        tools = {tool["name"]: tool for tool in self.gateway.list_tools()}
        names = list(tools)

        self.assertEqual(names.index(REVOKE_TOOL), names.index("browser.share_session") + 1)
        self.assertEqual(
            tools[REVOKE_TOOL]["annotations"],
            {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        )
        spec = self.gateway._registry.tools[REVOKE_TOOL]
        self.assertEqual((spec.pack, spec.governed_kind), ("session_admin", "destructive"))


class RevokeOwnershipTests(unittest.TestCase):
    """Through the real app: only an operator who may use the session ends its links."""

    def test_another_operators_revoke_is_404_and_ends_nothing(self) -> None:
        manager = main_module.manager
        session_id = "shared-by-alice"
        directory = Path(manager.settings.artifact_root) / session_id
        manager.sessions[session_id] = BrowserSession(
            id=session_id,
            name=session_id,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=directory,
            auth_dir=directory / "auth",
            upload_dir=directory / "uploads",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=directory / "trace.zip",
            owner="alice",
        )
        self.addCleanup(manager.sessions.pop, session_id, None)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service in (main_module.manager, main_module.job_queue, main_module.cron_service, main_module.maintenance):
            for method_name in ("startup", "shutdown"):
                stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        client = stack.enter_context(TestClient(main_module.app))
        identity = stack.enter_context(patch("app.session_ownership.get_current_operator"))
        token = main_module.share_manager.create_token(session_id, ttl_seconds=60)["token"]

        identity.return_value = BOB
        refused = client.delete(f"/sessions/{session_id}/share")

        self.assertEqual((refused.status_code, refused.json()["code"]), (404, "unknown_session"))
        self.assertTrue(main_module.share_manager.token_info(token)["valid"])
        identity.return_value = ALICE
        self.assertEqual(client.delete(f"/sessions/{session_id}/share").status_code, 200)
        self.assertFalse(main_module.share_manager.token_info(token)["valid"])


if __name__ == "__main__":
    unittest.main()
