"""The session replay page: what a visited page's URL may become in the operator's browser (AUTH-37).

The replay page renders the session's current URL as a link. A page sets its own URL, and a `javascript:` or `data:`
one clicked on the operator's own controller origin runs there, where a tokenless controller or an open token in the
page is reachable. Only an http(s) URL is a link, with no opener and no referrer; anything else is shown as text.
"""

from __future__ import annotations

import html as _html
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes._utils import is_http_link
from app.routes.session_diagnostics import create_session_diagnostics_router

SESSION = "s1"


def _replay(tmp_path: Path, url: object) -> str:
    manager = SimpleNamespace(
        sessions={SESSION: object()},
        list_audit_events=AsyncMock(return_value=[]),
        _session_summary=AsyncMock(return_value={"url": url, "status": "active", "title": "A page", "created_at": ""}),
    )
    app = FastAPI()
    app.include_router(
        create_session_diagnostics_router(manager=manager, settings=SimpleNamespace(artifact_root=str(tmp_path)))
    )
    response = TestClient(app).get(f"/sessions/{SESSION}/replay")
    assert response.status_code == 200, response.text
    return response.text


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
def test_a_url_that_is_not_http_is_plain_text(tmp_path: Path, url: str) -> None:
    body = _replay(tmp_path, url)

    assert "<a href=" not in body, body
    assert _html.escape(url) in body


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/?a=1&b=2",
        "http://example.com/",
        "HTTP://EXAMPLE.COM/",
        "HttpS://Example.com/",
    ],
)
def test_an_http_url_is_a_link_with_no_opener_and_no_referrer(tmp_path: Path, url: str) -> None:
    escaped = _html.escape(url)

    assert f'<a href="{escaped}" target="_blank" rel="noopener noreferrer">{escaped}</a>' in _replay(tmp_path, url)


def test_an_http_url_stays_escaped_in_the_link(tmp_path: Path) -> None:
    url = 'https://example.com/"><script>alert(1)</script>'

    body = _replay(tmp_path, url)

    assert "<script>alert(1)</script>" not in body
    assert "&quot;&gt;&lt;script&gt;" in body


@pytest.mark.parametrize("url", ["", None])
def test_a_session_with_no_url_shows_no_url_line(tmp_path: Path, url: object) -> None:
    assert "URL:" not in _replay(tmp_path, url)


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
