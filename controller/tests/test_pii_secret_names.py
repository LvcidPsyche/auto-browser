"""The secret-name rule: what it leaves alone, and what it keeps in memory.

A name is read by `names_a_secret`, which caches its answers. A page chooses the names in its own traffic,
so a name longer than a short bound must not be cached (1024 names of 16 KB each pinned 17 MB), and a
few names that read as secrets but are public or are a paging cursor (`public_key`, `next_page_token`)
must not be redacted.
"""

from __future__ import annotations

import gc
import json
import tracemalloc

import pytest

from app import pii_secret_fields
from app.pii_scrub import scrub_network_body
from app.pii_secret_fields import names_a_secret

FORM = "application/x-www-form-urlencoded"
JSON = "application/json"


# ── Long names are answered, not cached ────────────────────────────────────


def test_a_long_name_is_not_kept_alive_by_the_cache() -> None:
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for index in range(200):
            names_a_secret(f"token{index}_" + "a" * 16384)
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()

    # Cached, 200 names of 16 KB each stay alive: 3.3 MB.
    assert retained < 500_000, f"{retained} bytes retained"


@pytest.mark.parametrize("length", [255, 256, 257, 300, 5000])
def test_names_either_side_of_the_cache_bound_are_answered_alike(length: int) -> None:
    padding = "x" * (length - len("_password"))

    assert names_a_secret(padding + "_password")
    assert not names_a_secret(padding + "_nothing")
    assert names_a_secret(padding + "_password")  # asked twice: once cached or not, the same answer


def test_a_short_name_is_still_cached() -> None:
    pii_secret_fields._cached_names_a_secret.cache_clear()

    names_a_secret("refresh_token")
    names_a_secret("refresh_token")

    assert pii_secret_fields._cached_names_a_secret.cache_info().hits == 1


# ── Public keys and paging cursors ─────────────────────────────────────────

# `next_page_token` is a cursor into a list. `page_token` alone is not: a Facebook page access token is
# one, so only next, prev or previous before `page_token` makes the pair a cursor.
NOT_SECRET = [
    "public_key", "publicKey", "PUBLIC-KEY", "publishable_key", "publishableKey", "site_key", "siteKey",
    "next_page_token", "nextPageToken", "prev_page_token", "prevPageToken", "previous_page_token",
    "previousPageToken", "NEXT_PAGE_TOKEN", "user[site_key]", "page[next_page_token]",
]  # fmt: skip
STILL_SECRET = [
    "private_key", "privateKey", "secret_key", "api_key", "public_api_key", "site_secret_key", "site_secret",
    "publishable_key_secret", "page_access_token", "page_token_secret", "token_page", "access_token", "token",
    "page_secret", "key_public_token", "page_token", "pageToken", "page_token_value", "pageTokenValue",
    "PAGE_TOKEN", "user[page_token]", "next_token", "next_page_access_token", "next_secret_page_token",
    "next_access_token", "previous_refresh_token", "prev_page_pass", "next_page_secret", "fb_page_token",
    "user_page_token", "userPageToken",
]  # fmt: skip


@pytest.mark.parametrize("name", NOT_SECRET)
def test_public_key_and_cursor_names_are_not_secret_names(name: str) -> None:
    assert not names_a_secret(name)


@pytest.mark.parametrize("name", STILL_SECRET)
def test_names_that_only_look_like_those_are_still_secret_names(name: str) -> None:
    assert names_a_secret(name)


def test_a_json_public_key_and_a_cursor_are_stored_and_a_private_key_is_not() -> None:
    body = '{"publicKey":"pk-abc","nextPageToken":"CAEQAA","siteKey":"sk-xyz","privateKey":"opaque-private","n":1}'

    scrubbed, hits = scrub_network_body(body, JSON)

    assert json.loads(scrubbed) == {
        "publicKey": "pk-abc",
        "nextPageToken": "CAEQAA",
        "siteKey": "sk-xyz",
        "privateKey": "[REDACTED]",
        "n": 1,
    }
    assert len(hits) == 1


def test_a_form_public_key_and_a_cursor_are_stored_and_a_private_key_is_not() -> None:
    body = "public_key=pk-abc&next_page_token=CAEQAA&site_key=sk-xyz&private_key=opaque-private&n=1"

    scrubbed, hits = scrub_network_body(body, FORM)

    assert scrubbed == "public_key=pk-abc&next_page_token=CAEQAA&site_key=sk-xyz&private_key=%5BREDACTED%5D&n=1"
    assert len(hits) == 1


# A Facebook page access token is the value of `page_token`; it must not be stored.
PAGE_TOKEN = "EAABsbCS1iHgBAKZCZBqZCTokenValueOpaque123456"


@pytest.mark.parametrize("name", ["page_token", "pageToken", "page_token_value"])
def test_a_json_page_token_is_redacted_and_stays_json(name: str) -> None:
    body = json.dumps({"page_id": "1", name: PAGE_TOKEN})

    scrubbed, hits = scrub_network_body(body, JSON)

    assert json.loads(scrubbed) == {"page_id": "1", name: "[REDACTED]"}
    assert PAGE_TOKEN not in scrubbed
    assert hits


@pytest.mark.parametrize("name", ["page_token", "pageToken", "page_token_value"])
def test_a_form_page_token_is_redacted(name: str) -> None:
    # A value of 30 base64 characters after `token=` is also taken, name and all, by the generic pattern.
    scrubbed, hits = scrub_network_body(f"page_id=1&{name}={PAGE_TOKEN}", FORM)

    assert PAGE_TOKEN not in scrubbed
    assert scrubbed.startswith("page_id=1&page")
    assert hits


@pytest.mark.parametrize("name", ["page_token", "pageToken", "page_token_value"])
def test_a_short_form_page_token_loses_its_value_and_keeps_its_name(name: str) -> None:
    scrubbed, hits = scrub_network_body(f"page_id=1&{name}=CAEQAA", FORM)

    assert scrubbed == f"page_id=1&{name}=%5BREDACTED%5D"
    assert hits


# ── The word `pass` ────────────────────────────────────────────────────────

PASS_NAMES = ["pass", "db_pass", "smtp_pass", "PASS", "wifiPass", "user_pass", "pass_word", "user[pass]"]
# Names that hold the letters but not the word: `bypass` and `passed` are ordinary.
NOT_PASS_NAMES = ["bypass", "passed", "bypass_cache", "passenger_count", "compass", "passthrough", "surpass"]
# A name that does hold the word as a word is a secret, whatever else it says: `boarding_pass` is read as one.
OVER_REDACTED = ["boarding_pass", "pass_through"]


@pytest.mark.parametrize("name", PASS_NAMES + OVER_REDACTED)
def test_a_name_with_the_word_pass_is_a_secret_name(name: str) -> None:
    assert names_a_secret(name)


@pytest.mark.parametrize("name", NOT_PASS_NAMES)
def test_a_name_with_the_letters_pass_inside_a_longer_word_is_not(name: str) -> None:
    assert not names_a_secret(name)
