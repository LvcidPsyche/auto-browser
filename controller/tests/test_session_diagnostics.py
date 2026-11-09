"""The session replay page's URL line (AUTH-37).

The page read a key neither a session summary nor a stored record has (`url`, where they say `current_url`), so its
URL line never rendered. It now shows the session's URL, and a page sets that URL itself: only an http(s) one is a
link, with no opener and no referrer, and anything else is shown as text, or a `javascript:` one would run on the
controller's own origin if clicked.
"""

from __future__ import annotations

import html as _html
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager, BrowserSession
from app.models import SessionRecord
from app.routes._utils import is_http_link
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.utils import UTC
from tests.test_browser_manager_create_session import FakePage, _settings

SESSION = "s1"


def _record(url: str) -> SessionRecord:
    """A closed session's stored record, whose current_url the page reads through model_dump()."""
    return SessionRecord(
        id=SESSION,
        name="closed-session",
        created_at="2026-11-01T10:00:00+00:00",
        updated_at="2026-11-01T10:05:00+00:00",
        status="closed",
        current_url=url,
        title="A closed page",
        artifact_dir="/tmp/artifacts",
        takeover_url="http://127.0.0.1:6080/vnc.html",
        remote_access={},
    )


def _replay(tmp_path: Path, url: object, shape: str = "live") -> str:
    """The page for a live session (a real summary's keys) or a closed one (a stored record), at this URL."""
    if shape == "live":
        manager = SimpleNamespace(
            sessions={SESSION: object()},
            list_audit_events=AsyncMock(return_value=[]),
            _session_summary=AsyncMock(
                return_value={
                    "current_url": url,
                    "status": "active",
                    "title": "A page",
                    "created_at": "2026-11-01T10:00:00+00:00",
                }
            ),
        )
    else:
        manager = SimpleNamespace(
            sessions={},
            list_audit_events=AsyncMock(return_value=[]),
            session_store=SimpleNamespace(get=AsyncMock(return_value=_record(str(url)))),
        )
    app = FastAPI()
    app.include_router(
        create_session_diagnostics_router(manager=manager, settings=SimpleNamespace(artifact_root=str(tmp_path)))
    )
    response = TestClient(app).get(f"/sessions/{SESSION}/replay")
    assert response.status_code == 200, response.text
    return response.text


@pytest.fixture(params=["live", "closed"])
def shape(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        " javascript:alert(1)",
        "\tjavascript:alert(1)",
        "java\nscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "about:blank",
        "//evil.example/path",
        "/relative/path",
    ],
)
def test_a_url_that_is_not_http_is_plain_text(tmp_path: Path, url: str, shape: str) -> None:
    body = _replay(tmp_path, url, shape)

    assert "<a href=" not in body, body
    assert f"<span>URL: {_html.escape(url)}</span>" in body


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/?a=1&b=2",
        "http://example.com/",
        "HTTP://EXAMPLE.COM/",
        "HttpS://Example.com/",
    ],
)
def test_an_http_url_is_a_link_with_no_opener_and_no_referrer(tmp_path: Path, url: str, shape: str) -> None:
    escaped = _html.escape(url)

    assert f'<a href="{escaped}" target="_blank" rel="noopener noreferrer">{escaped}</a>' in _replay(
        tmp_path, url, shape
    )


def test_an_http_url_stays_escaped_in_the_link(tmp_path: Path, shape: str) -> None:
    url = 'https://example.com/"><script>alert(1)</script>'

    body = _replay(tmp_path, url, shape)

    assert "<script>alert(1)</script>" not in body
    assert "&quot;&gt;&lt;script&gt;" in body


def test_a_session_with_no_url_shows_no_url_line(tmp_path: Path, shape: str) -> None:
    assert "URL:" not in _replay(tmp_path, "", shape)


def test_a_summary_with_a_null_url_shows_no_url_line(tmp_path: Path) -> None:
    assert "URL:" not in _replay(tmp_path, None)


class TheRealSummaryShapeTests(unittest.IsolatedAsyncioTestCase):
    """The page against a real manager's summary, so a key it reads that nothing writes cannot pass again."""

    async def test_a_live_sessions_url_reaches_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = BrowserManager(_settings(Path(tmp)))
            page = FakePage()
            page.url = "https://example.com/page"
            artifact_dir = Path(tmp) / "artifacts" / SESSION
            manager.sessions[SESSION] = BrowserSession(
                id=SESSION,
                name=SESSION,
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=page,  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=artifact_dir / "auth",
                upload_dir=artifact_dir / "uploads",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
            )
            app = FastAPI()
            app.include_router(create_session_diagnostics_router(manager=manager, settings=manager.settings))

            response = TestClient(app).get(f"/sessions/{SESSION}/replay")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('<a href="https://example.com/page" target="_blank"', response.text)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com", True),
        ("http://example.com", True),
        ("HTTPS://EXAMPLE.COM", True),
        ("  https://example.com  ", True),
        ("https:", False),
        ("https//example.com", False),
        ("javascript:alert(1)", False),
        ("data:text/html,x", False),
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_is_http_link(url: object, expected: bool) -> None:
    assert is_http_link(url) is expected
