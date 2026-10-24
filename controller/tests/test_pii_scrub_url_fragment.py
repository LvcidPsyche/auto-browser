"""The fragment of a URL, scrubbed like its query.

An OAuth implicit grant and an OIDC `response_mode=fragment` response come back as
`Location: https://app.test/cb#access_token=...&id_token=...`: the secrets are in the fragment, which
only the query used to be scrubbed for.
"""

from __future__ import annotations

import pytest

from app import pii_scrub
from app.pii_scrub import PiiScrubber

# Assembled from parts so secret scanners do not flag a synthetic fixture.
JWT = ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "c2lnbmF0dXJlLXZhbHVl"])


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://app.test/cb#access_token=Opaque-Access-123&expires_in=3600&state=xyz",
            "https://app.test/cb#access_token=%5BREDACTED%5D&expires_in=3600&state=xyz",
        ),
        (
            f"https://app.test/cb#id_token={JWT}&state=s",
            "https://app.test/cb#id_token=[REDACTED]&state=s",
        ),
        (
            "https://app.test/cb#code=Opaque-Code-1234567",
            "https://app.test/cb#[REDACTED]",
        ),
        (
            "https://app.test/cb#session=opaque&refresh_token=Opaque-Refresh-9",
            "https://app.test/cb#session=opaque&refresh_token=%5BREDACTED%5D",
        ),
        (
            "https://app.test/cb#user%5Bpassword%5D=opaque",
            "https://app.test/cb#user%5Bpassword%5D=%5BREDACTED%5D",
        ),
        (
            "https://app.test/cb#email=ops%40example.com",
            "https://app.test/cb#email=%5BREDACTED%5D",
        ),
        (
            "https://app.test/cb?state=s#access_token=Opaque-Access-123",
            "https://app.test/cb?state=s#access_token=%5BREDACTED%5D",
        ),
        (
            "https://app.test/cb?email=ops%40example.com#access_token=Opaque-Access-123",
            "https://app.test/cb?email=%5BREDACTED%5D#access_token=%5BREDACTED%5D",
        ),
        (
            "https://app.test/#/inbox?token=Opaque-Route-Token",
            "https://app.test/#/inbox?token=%5BREDACTED%5D",
        ),
    ],
    ids=[
        "access-token",
        "id-token",
        "code",
        "refresh-token-after-a-plain-pair",
        "bracketed-secret-name",
        "email",
        "query-and-fragment-one-secret",
        "query-and-fragment-both",
        "client-route-with-a-token",
    ],
)
def test_the_fragment_is_scrubbed_pair_by_pair_like_a_query(url: str, expected: str) -> None:
    scrubbed, hits = pii_scrub.scrub_url_query(url)

    assert scrubbed == expected
    assert hits
    assert "Opaque" not in scrubbed and "ops" not in scrubbed


@pytest.mark.parametrize(
    "url",
    [
        "https://app.test/page#section-2",
        "https://app.test/page#/users/42",
        "https://app.test/page?q=shoes#results",
        "https://app.test/page#tab=2&view=list",
        "https://app.test/page#",
        "https://app.test/page?",
        "https://app.test/page",
    ],
)
def test_a_fragment_with_nothing_to_redact_comes_back_untouched(url: str) -> None:
    assert pii_scrub.scrub_url_query(url) == (url, [])


def test_hits_for_the_query_and_the_fragment_are_both_reported() -> None:
    _, hits = pii_scrub.scrub_url_query("https://app.test/cb?email=ops%40example.com#access_token=Opaque-Access-123")

    assert {hit["pattern"] for hit in hits} == {"email", "api_key_param"}
    assert all(hit["preview"] != "Opaque" for hit in hits)


def test_the_replacement_and_the_enabled_patterns_apply_to_the_fragment() -> None:
    url = "https://app.test/cb#access_token=Opaque-Access-123&email=ops%40example.com"

    only_email, _ = pii_scrub.scrub_url_query(url, replacement="***", enabled_patterns={"email"})
    only_secret, _ = pii_scrub.scrub_url_query(url, replacement="***", enabled_patterns={"api_key_param"})

    assert only_email == "https://app.test/cb#access_token=Opaque-Access-123&email=%2A%2A%2A"
    assert only_secret == "https://app.test/cb#access_token=%2A%2A%2A&email=ops%40example.com"


def test_the_scrubber_service_scrubs_the_fragment_unless_network_scrubbing_is_off() -> None:
    url = "https://app.test/cb#access_token=Opaque-Access-123"

    scrubbed, hits = PiiScrubber().url(url)

    assert scrubbed == "https://app.test/cb#access_token=%5BREDACTED%5D"
    assert hits
    assert PiiScrubber(network_enabled=False).url(url) == (url, [])
    assert PiiScrubber(enabled=False).url(url) == (url, [])


def test_a_fragment_costs_the_same_number_of_pattern_passes_whatever_its_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = pii_scrub.find_pii_spans
    monkeypatch.setattr(pii_scrub, "find_pii_spans", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))

    def passes(pairs: int) -> int:
        fragment = "&".join(f"k{index}=v%41" for index in range(pairs)) + "&email=ops%40example.com"
        calls.clear()
        pii_scrub.scrub_url_query("https://x.test/p#" + fragment)
        return len(calls)

    small, large = passes(4), passes(2000)

    assert large == small
    assert large <= 2
