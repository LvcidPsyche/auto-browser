"""Request bodies that are text get scrubbed whatever their content type says.

`scrub_network_body` only scrubbed when the content type named json, text,
form, xml, javascript or graphql. A fetch with a string body sends no content
type header from Playwright's point of view, and YAML has its own type, so
those bodies were stored in the inspector verbatim with `pii_redacted: False`.
"""

from __future__ import annotations

import pytest

from app.network_inspector import NetworkInspector
from app.pii_scrub import PiiScrubber, scrub_network_body
from tests.test_network_inspector import FakeRequest, FakeResponse

SECRET_EMAIL = "victim@example.com"
SECRET_BODY = f"owner: {SECRET_EMAIL}\npassword: hunter2xyz\n"


def _inspector() -> NetworkInspector:
    return NetworkInspector("session-body", scrubber=PiiScrubber(enabled_patterns={"email", "password_field"}))


async def _logged_entry(request: FakeRequest, response: FakeResponse | None = None) -> dict:
    inspector = _inspector()
    await inspector._handle_request(request)
    if response is not None:
        await inspector._handle_response(response)
    await inspector._handle_request_finished(request)
    return inspector.entries()[0]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"content-type": "application/yaml"},
        {"content-type": "application/x-yaml"},
        {"content-type": "application/vnd.example.v1"},
    ],
    ids=["no-content-type", "yaml", "x-yaml", "unknown-type"],
)
async def test_text_request_body_is_scrubbed_whatever_its_type(headers: dict[str, str]) -> None:
    request = FakeRequest(method="POST", headers=headers, post_data=SECRET_BODY)

    entry = await _logged_entry(request)

    assert SECRET_EMAIL not in entry["request_body"]
    assert "hunter2xyz" not in entry["request_body"]
    assert entry["pii_redacted"] is True


@pytest.mark.parametrize("content_type", ["application/octet-stream", "image/png", "application/pdf"])
async def test_known_binary_request_body_stays_unscrubbed(content_type: str) -> None:
    request = FakeRequest(method="POST", headers={"content-type": content_type}, post_data=SECRET_BODY)

    entry = await _logged_entry(request)

    assert entry["request_body"] == SECRET_BODY
    assert entry["pii_redacted"] is False


def test_bytes_without_a_type_scrub_when_utf8_and_stay_when_not() -> None:
    text, hits = scrub_network_body(SECRET_BODY.encode(), "", enabled_patterns={"email"})
    assert SECRET_EMAIL.encode() not in text
    assert hits

    binary = b"\xff\xfe\x00" + SECRET_EMAIL.encode()  # not valid UTF-8
    unchanged, no_hits = scrub_network_body(binary, "", enabled_patterns={"email"})
    assert unchanged == binary
    assert no_hits == []


def test_yaml_content_types_are_text() -> None:
    for content_type in ("application/yaml", "application/x-yaml", "text/yaml; charset=utf-8"):
        scrubbed, hits = scrub_network_body(SECRET_BODY.encode(), content_type, enabled_patterns={"email"})
        assert SECRET_EMAIL.encode() not in scrubbed, content_type
        assert hits


@pytest.mark.parametrize("content_type", ["application/yaml", "application/html"])
def test_a_text_type_is_scrubbed_even_when_the_bytes_are_not_utf8(content_type: str) -> None:
    # The UTF-8 fallback covers an untyped body; a type that names text is what
    # covers one in another encoding (a Latin-1 YAML file), which the fallback
    # would leave verbatim.
    latin1 = "café: ".encode("latin-1") + SECRET_EMAIL.encode()
    scrubbed, hits = scrub_network_body(latin1, content_type, enabled_patterns={"email"})
    assert SECRET_EMAIL.encode() not in scrubbed
    assert hits


async def test_response_with_an_html_type_that_has_no_text_marker_is_scrubbed() -> None:
    """The response path stores a body when the type names html, but the scrubber's
    own list did not: `application/html` was stored verbatim."""
    request = FakeRequest(url="https://example.com/page", method="GET")
    response = FakeResponse(
        request,
        headers={"content-type": "application/html"},
        body=f"<p>{SECRET_EMAIL}</p>".encode(),
    )

    entry = await _logged_entry(request, response)

    assert SECRET_EMAIL not in entry["response_body"]
    assert entry["pii_redacted"] is True


async def test_response_in_a_type_the_inspector_does_not_capture_is_not_stored() -> None:
    """Pins the response path's current shape: a body it does not recognise as text
    is dropped, never stored verbatim (so yaml responses are absent, not leaked)."""
    request = FakeRequest(url="https://example.com/cfg", method="GET")
    response = FakeResponse(request, headers={"content-type": "application/yaml"}, body=SECRET_BODY.encode())

    entry = await _logged_entry(request, response)

    assert entry["response_body"] is None
