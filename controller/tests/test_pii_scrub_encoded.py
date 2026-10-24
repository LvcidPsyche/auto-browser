"""PII that arrives encoded: form bodies, JSON string escapes and request URLs.

The patterns match raw text, so `ops%40example.com`, `4111+1111+1111+1111` and
the JSON text `ops\\u0040example.com` (six characters for the `@`) never looked
like an email or a card, and were stored verbatim by the network inspector.
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse

import pytest

from app import pii_scrub
from app.pii_scrub import PiiScrubber, scrub_network_body
from app.pii_secret_fields import json_secret_values, names_a_secret

FORM = "application/x-www-form-urlencoded"
HIT_KEYS = {"pattern", "offset", "length", "preview"}


# ── URL-encoded form bodies ────────────────────────────────────────────────


def test_form_body_percent_encoded_email_is_scrubbed() -> None:
    scrubbed, hits = scrub_network_body("email=ops%40example.com", FORM)

    assert scrubbed == "email=%5BREDACTED%5D"
    assert [hit["pattern"] for hit in hits] == ["email"]
    assert set(hits[0]) == HIT_KEYS


def test_form_body_card_number_written_with_plus_signs_is_scrubbed() -> None:
    scrubbed, hits = scrub_network_body("card=4111+1111+1111+1111&name=Ada", FORM)

    assert "4111" not in scrubbed
    assert scrubbed.endswith("&name=Ada")
    assert [hit["pattern"] for hit in hits] == ["credit_card"]


def test_form_body_keeps_pair_order_and_blank_values() -> None:
    body = "first=1&email=ops%40example.com&blank=&note=hello+world&last=z"

    scrubbed, hits = scrub_network_body(body, FORM)

    assert scrubbed == "first=1&email=%5BREDACTED%5D&blank=&note=hello+world&last=z"
    assert len(hits) == 1


def test_form_body_keys_are_scrubbed_too() -> None:
    scrubbed, hits = scrub_network_body("ops%40example.com=1&plain=2", FORM)

    assert "ops" not in scrubbed
    assert scrubbed.endswith("=1&plain=2")
    assert hits


@pytest.mark.parametrize(
    "text",
    [
        "ops%40example.com",
        "4111+1111+1111+1111",
        "ops%40example.com&flag",
        "flag&ops%40example.com",
        # The raw pass removes the only `=`, which used to end the decoded pass early.
        "ops%40example.com&password=hunter2xyz",
    ],
)
def test_text_with_no_equals_sign_is_decoded_and_scrubbed_too(text: str) -> None:
    scrubbed, hits = scrub_network_body(text, FORM)
    assert "ops" not in scrubbed and "4111" not in scrubbed
    assert hits

    url, url_hits = pii_scrub.scrub_url_query(f"https://x.test/p?{text}")
    assert "ops" not in url and "4111" not in url
    assert url.startswith("https://x.test/p?")
    assert url_hits


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("ops%40example.com&flag", "%5BREDACTED%5D&flag"),
        ("flag&ops%40example.com&other", "flag&%5BREDACTED%5D&other"),
        ("a=1&&b=2&e=ops%40example.com&", "a=1&&b=2&e=%5BREDACTED%5D&"),
    ],
)
def test_only_the_matched_stretch_is_rewritten_so_key_only_and_empty_pairs_survive(body: str, expected: str) -> None:
    assert scrub_network_body(body, FORM)[0] == expected
    assert pii_scrub.scrub_url_query(f"https://x.test/p?{body}")[0] == f"https://x.test/p?{expected}"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # A key that holds an `=` once decoded stays one key.
        ("a%3Db=ops%40example.com&z=1", "a%3Db=%5BREDACTED%5D&z=1"),
        # Bytes that are not UTF-8 are not rewritten (the page may send latin-1) when a redaction fires.
        ("note=caf%E9&e=ops%40example.com", "note=caf%E9&e=%5BREDACTED%5D"),
        ("note=caf%E9+au+lait&e=ops%40example.com", "note=caf%E9+au+lait&e=%5BREDACTED%5D"),
        # Characters the encoder would have escaped differently are left as they came.
        ("tilde=~&star=*&e=ops%40example.com", "tilde=~&star=*&e=%5BREDACTED%5D"),
    ],
)
def test_pairs_without_a_match_keep_the_bytes_they_arrived_with(body: str, expected: str) -> None:
    assert scrub_network_body(body, FORM)[0] == expected
    assert pii_scrub.scrub_url_query(f"https://x.test/p?{body}")[0] == f"https://x.test/p?{expected}"


def test_a_match_that_starts_inside_a_percent_run_takes_the_whole_run() -> None:
    # The email starts at the `+` that ends the first run; half a run cannot be redacted on its own.
    scrubbed, hits = scrub_network_body("x=%C3%A9%2Bops%40example.com&y=1", FORM)

    assert scrubbed == "x=%5BREDACTED%5D&y=1"
    assert [hit["pattern"] for hit in hits] == ["email"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("api_key=abcdefgh&password=xyz12345", "api_key=%5BREDACTED%5D&password=%5BREDACTED%5D"),
        ("access_token=abcdefgh123&password=xyz12345&n=1", "access_token=%5BREDACTED%5D&password=%5BREDACTED%5D&n=1"),
        ("n=1&client_secret=abcdefgh1234&passwd=xyz12345", "n=1&client_secret=%5BREDACTED%5D&passwd=%5BREDACTED%5D"),
        ("refresh_token=abcdefgh&note=keep&pin=1234", "refresh_token=%5BREDACTED%5D&note=keep&pin=%5BREDACTED%5D"),
        # Values too short for any pattern: only the names find them, and they were never at risk.
        ("api_key=abc&password=xyz", "api_key=%5BREDACTED%5D&password=%5BREDACTED%5D"),
        ("n=1&api%5Fkey=abcdefgh&password=xyz12345", "n=1&api%5Fkey=%5BREDACTED%5D&password=%5BREDACTED%5D"),
    ],
)
def test_a_key_parameter_does_not_swallow_the_parameter_after_it(body: str, expected: str) -> None:
    # `api_key_param` takes `&` as part of a value; it used to eat the next name (`&password`) as well,
    # leaving `[REDACTED]=xyz12345`: the password stored raw under no name at all.
    scrubbed, hits = scrub_network_body(body, FORM)

    assert scrubbed == expected
    assert hits
    url, url_hits = pii_scrub.scrub_url_query(f"https://x.test/p?{body}")
    assert url == f"https://x.test/p?{expected}"
    assert url_hits


def test_a_password_in_a_form_keeps_its_name_and_loses_its_value() -> None:
    scrubbed, hits = scrub_network_body("user=ada&password=hunter2xyz&next=1", FORM)

    assert scrubbed == "user=ada&password=%5BREDACTED%5D&next=1"
    assert [hit["pattern"] for hit in hits] == ["password_field"]


@pytest.mark.parametrize("body", ["api_key=abcdefghijkl&x=1", "n=1&api%5Fkey=abcdefghijkl", "password=hunter2xyz"])
def test_a_hit_on_a_secret_value_does_not_preview_the_value(body: str) -> None:
    _, hits = scrub_network_body(body, FORM)

    assert hits
    assert all("abcdef" not in str(hit) and "hunter" not in str(hit) for hit in hits)


def test_matches_do_not_run_on_from_one_parameter_into_the_next() -> None:
    # `api_key_param` counts `&` as part of a value; in the decoded view the next key is out of reach.
    scrubbed, _ = scrub_network_body("api%5Fkey=abcdefgh%2112345678&note=x", FORM)

    assert scrubbed.endswith("&note=x")
    assert "abcdefgh" not in scrubbed


def test_text_with_no_equals_sign_and_no_pii_comes_back_untouched() -> None:
    for text in ("debug", "debug&verbose", "caf%C3%A9&x+y", "flag&"):
        assert scrub_network_body(text, FORM) == (text, [])
        url = f"https://x.test/p?{text}"
        assert pii_scrub.scrub_url_query(url) == (url, [])


def test_form_body_without_pii_comes_back_untouched() -> None:
    body = "q=caf%C3%A9&page=2&blank="

    assert scrub_network_body(body, FORM) == (body, [])


def test_form_body_that_is_not_pair_shaped_still_gets_the_plain_scrub() -> None:
    scrubbed, hits = scrub_network_body("write to ops@example.com please", FORM)

    assert scrubbed == "write to [REDACTED] please"
    assert [hit["pattern"] for hit in hits] == ["email"]


def test_form_body_bytes_in_bytes_out() -> None:
    scrubbed, hits = scrub_network_body(b"email=ops%40example.com&n=1", FORM)

    assert scrubbed == b"email=%5BREDACTED%5D&n=1"
    assert hits

    untouched = b"n=1&m=2"
    assert scrub_network_body(untouched, FORM) == (untouched, [])


def test_form_body_still_catches_patterns_that_need_the_key() -> None:
    # `password=...` only matches with the key beside the value, which the
    # pair-by-pair scrub does not see.
    scrubbed, hits = scrub_network_body("user=ada&password=hunter2xyz", FORM)

    assert "hunter2xyz" not in scrubbed
    assert hits


def test_form_body_scrub_honours_replacement_and_enabled_patterns() -> None:
    body = "email=ops%40example.com&card=4111+1111+1111+1111"

    scrubbed, hits = scrub_network_body(body, FORM, replacement="***", enabled_patterns={"email"})

    assert scrubbed == "email=%2A%2A%2A&card=4111+1111+1111+1111"
    assert [hit["pattern"] for hit in hits] == ["email"]


def test_form_body_keeps_a_raw_text_match_that_sits_beside_an_encoded_hit() -> None:
    # Only the raw pass can see this token: decoded, its `+` signs become spaces and the run
    # is too short for the pattern, and `monkey` does not name a secret. Removing the raw pass
    # leaves the token in the output, which is what this test is for.
    body = "email=ops%40example.com&monkey=AAAAAAAAAAAAAAAAAAAA+BBBBBBBBBBBBBBBBBBBBBB"

    scrubbed, hits = scrub_network_body(body, FORM)

    assert "AAAA" not in scrubbed
    assert "ops" not in scrubbed
    assert {hit["pattern"] for hit in hits} == {"email", "generic_b64_secret"}


def test_form_body_raw_pass_is_what_catches_a_token_that_decoding_breaks_up() -> None:
    scrubbed, hits = scrub_network_body("monkey=AAAAAAAAAAAAAAAAAAAA+BBBBBBBBBBBBBBBBBBBBBB", FORM)

    assert "AAAA" not in scrubbed
    assert [hit["pattern"] for hit in hits] == ["generic_b64_secret"]


def test_url_query_raw_pass_is_what_catches_a_token_that_decoding_breaks_up() -> None:
    scrubbed, hits = pii_scrub.scrub_url_query("https://x.test/p?monkey=AAAAAAAAAAAAAAAAAAAA+BBBBBBBBBBBBBBBBBBBBBB")

    assert "AAAA" not in scrubbed
    assert [hit["pattern"] for hit in hits] == ["generic_b64_secret"]


def test_form_body_bracketed_secret_parameter_names_are_redacted() -> None:
    body = "user%5Bemail%5D=ops%40example.com&user%5Bpassword%5D=hunter2xyz&api%5Fkey=abcdefgh12345678&note=x"

    scrubbed, hits = scrub_network_body(body, FORM)

    assert "ops" not in scrubbed
    assert "hunter2xyz" not in scrubbed
    assert "abcdefgh" not in scrubbed
    assert scrubbed.endswith("&note=x")
    assert "password_field" in {hit["pattern"] for hit in hits}


@pytest.mark.parametrize(
    "spelling",
    ["to%6ben", "user%5Btoken%5D", "token", "user[password][0]"],
)
def test_secret_named_parameter_is_redacted_whole_even_when_a_pattern_hit_part_of_it(spelling: str) -> None:
    # The email is a partial hit; the name says the whole value is a secret, so what follows it goes too.
    body = f"{spelling}=ops%40example.com%26TAILSECRET&note=x"

    scrubbed, hits = scrub_network_body(body, FORM)

    assert "TAILSECRET" not in scrubbed
    assert "ops" not in scrubbed
    assert scrubbed.endswith("=%5BREDACTED%5D&note=x")
    assert {"email", "password_field"} <= {hit["pattern"] for hit in hits}

    url, url_hits = pii_scrub.scrub_url_query(f"https://x.test/p?{spelling}=ops%40example.com%26TAILSECRET")
    assert "TAILSECRET" not in url
    assert {"email", "password_field"} <= {hit["pattern"] for hit in url_hits}


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "db_password",
        "x_token",
        "my_secret",
        "stripe_key",
    ],
)
def test_every_kind_of_secret_name_is_found_by_the_prefilter(name: str) -> None:
    # `hunter2xyz` matches no value pattern, and the encoded spelling hides the name from the raw pass,
    # so the name is what has to find it, through the hint that skips texts with no name in them.
    for spelling in (name, name.upper(), name.replace("_", "%5F"), "".join(f"%{ord(c):02X}" for c in name)):
        scrubbed, hits = scrub_network_body(f"a=1&{spelling}=hunter2xyz", FORM)

        assert "hunter2xyz" not in scrubbed, spelling
        assert scrubbed.startswith("a=1&"), spelling
        assert hits, spelling


def test_secret_named_parameter_with_plus_signs_around_a_partial_hit_is_redacted_whole() -> None:
    scrubbed, _ = scrub_network_body("token=my+secret+ops%40example.com", FORM)

    assert scrubbed == "token=%5BREDACTED%5D"


def test_secret_named_parameter_whose_value_is_all_one_hit_is_counted_once() -> None:
    scrubbed, hits = scrub_network_body("token=ops%40example.com", FORM)

    assert scrubbed == "token=%5BREDACTED%5D"
    assert [hit["pattern"] for hit in hits] == ["email"]


def test_secret_name_redaction_needs_the_password_field_pattern_to_be_enabled() -> None:
    scrubbed, hits = scrub_network_body("token=ops%40example.com%26TAILSECRET", FORM, enabled_patterns={"email"})

    assert "TAILSECRET" in scrubbed
    assert [hit["pattern"] for hit in hits] == ["email"]


@pytest.mark.parametrize(
    "name",
    [
        "password[]",
        "password[0]",
        "token[0]",
        "secret[]",
        "user[password][]",
        "user[password][0]",
        "api_key[]",
        "auth[access_token][12]",
        "items[0][password]",
    ],
)
def test_array_style_secret_parameter_names_are_redacted(name: str) -> None:
    for spelling in (name, urllib.parse.quote(name, safe="")):
        scrubbed, hits = scrub_network_body(f"{spelling}=hunter2xyz&note=x", FORM)

        assert "hunter2xyz" not in scrubbed, spelling
        assert scrubbed.endswith("&note=x"), spelling
        assert "password_field" in {hit["pattern"] for hit in hits}, spelling

        url, url_hits = pii_scrub.scrub_url_query(f"https://x.test/p?{spelling}=hunter2xyz")
        assert "hunter2xyz" not in url, spelling
        assert url_hits


NOT_SECRET_NAMES = [
    "colors[]",
    "page[0]",
    "monkey[]",
    "tokens[]",
    "user[name][1]",
    "spin",
    "pinned",
    "keyboard",
    "tokenizer_name",
    "secretary",
    "passage",
    "max_tokens",
    "keywords",
]
SECRET_NAMES = [
    "password_confirmation",
    "passwordConfirmation",
    "password2",
    "password[new]",
    "password[confirmation]",
    "new_password",
    "user[password_confirmation]",
    "user[passwordConfirmation]",
    "passphrase",
    "cvv",
    "cvc",
    "csc",
    "pin",
    "card[cvv]",
    "PIN",
    "accessToken",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "clientSecret",
    "secret_key",
    "apiKey",
    "x-api-key",
]


@pytest.mark.parametrize("name", NOT_SECRET_NAMES)
def test_names_that_are_not_secrets_are_left_alone(name: str) -> None:
    body = f"{urllib.parse.quote(name, safe='')}=hunter2xyz&note=x"

    assert scrub_network_body(body, FORM) == (body, [])
    url = f"https://x.test/p?{body}"
    assert pii_scrub.scrub_url_query(url) == (url, [])


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_a_name_with_a_secret_word_in_it_has_its_value_redacted(name: str) -> None:
    for spelling in (name, urllib.parse.quote(name, safe="")):
        scrubbed, hits = scrub_network_body(f"a=1&{spelling}=hunter2xyz", FORM)

        assert "hunter2xyz" not in scrubbed, spelling
        assert scrubbed.startswith("a=1&"), spelling
        assert hits, spelling

        url, url_hits = pii_scrub.scrub_url_query(f"https://x.test/p?{spelling}=hunter2xyz")
        assert "hunter2xyz" not in url, spelling
        assert url_hits, spelling


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_names_a_secret_accepts_every_secret_name(name: str) -> None:
    assert names_a_secret(name)


@pytest.mark.parametrize("name", NOT_SECRET_NAMES)
def test_names_a_secret_rejects_every_other_name(name: str) -> None:
    assert not names_a_secret(name)


# ── JSON string escapes ────────────────────────────────────────────────────


def test_json_unicode_escaped_email_is_scrubbed_and_the_body_stays_valid_json() -> None:
    body = r'{"email":"ops\u0040example.com","n":1}'

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert scrubbed == '{"email":"[REDACTED]","n":1}'
    assert json.loads(scrubbed) == {"email": "[REDACTED]", "n": 1}
    assert [hit["pattern"] for hit in hits] == ["email"]


def test_json_bytes_in_bytes_out() -> None:
    scrubbed, hits = scrub_network_body(rb'{"email":"ops\u0040example.com"}', "application/json; charset=utf-8")

    assert scrubbed == b'{"email":"[REDACTED]"}'
    assert hits


@pytest.mark.parametrize("content_type", ["text/javascript", "application/javascript", "application/vnd.api+json"])
def test_javascript_and_json_flavoured_types_decode_escapes(content_type: str) -> None:
    scrubbed, hits = scrub_network_body(r'x = {"email":"ops\u0040example.com"};', content_type)

    assert "ops" not in scrubbed
    assert hits


def test_json_body_with_an_escaped_quote_stays_valid_json() -> None:
    # \u0022 is a quote: decoding it would end the string early.
    body = r'{"note":"say \"hi\" \u0022now\u0022","email":"ops\u0040example.com"}'

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert hits
    assert json.loads(scrubbed) == {"note": 'say "hi" "now"', "email": "[REDACTED]"}


def test_json_special_code_points_stay_escaped_while_others_are_decoded() -> None:
    body = r'{"a":"\u005c\u000a\u0022","b":"caf\u00e9","email":"ops\u0040example.com"}'

    scrubbed, _ = scrub_network_body(body, "application/json")

    assert r"\u005c\u000a\u0022" in scrubbed
    assert json.loads(scrubbed) == {"a": '\\\n"', "b": "café", "email": "[REDACTED]"}


def test_json_escaped_backslash_before_u_is_not_an_escape() -> None:
    # `\\u0040` is a backslash followed by the text u0040; decoding it would
    # leave a lone backslash and break the JSON.
    body = r'{"path":"C:\\u0040dir","email":"ops\u0040example.com"}'

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert hits
    assert json.loads(scrubbed) == {"path": "C:\\u0040dir", "email": "[REDACTED]"}


def test_json_escapes_of_characters_that_end_a_match_stay_escaped() -> None:
    # `&`, `'` and a space end a `password=` match, so writing them out would cut
    # the redaction short; an HTML-safe JSON encoder escapes exactly these.
    scrubbed, hits = scrub_network_body(r'{"password":"p\u0026ssw0rd!","x":"it\u0027s a"}', "application/json")

    assert "ssw0rd" not in scrubbed
    assert "p&" not in scrubbed
    assert hits


def _j(text: str) -> str:
    """JSON text with `¦` standing for the backslash that starts an escape (so that this file holds none)."""
    return text.replace("¦", chr(92))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # An escaped space between the digit groups, which the old token-only decode left escaped.
        ('{"c":"4111¦u00201111¦u00201111¦u00201111"}', '{"c":"[REDACTED]"}'),
        ('{"h":"Bearer¦u0020abcdefghijklmno"}', '{"h":"[REDACTED]"}'),
        # The token goes on past an escaped `/`.
        ('{"h":"Bearer abcdefgh¦u002fTAILSECRET"}', '{"h":"[REDACTED]"}'),
        ('{"h":"Bearer abc¦u002fdef¦u002bghiTAILSECRET"}', '{"h":"[REDACTED]"}'),
    ],
)
def test_json_escapes_of_spaces_and_slashes_are_written_out_before_matching(body: str, expected: str) -> None:
    scrubbed, hits = scrub_network_body(_j(body), "application/json")

    assert scrubbed == expected
    assert json.loads(scrubbed)
    assert hits


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # PHP's json_encode writes every slash as the short escape `\/`; a token goes on past it.
        ('{"h":"Bearer abc¦/defSECRETTAIL+ghi=="}', '{"h":"[REDACTED]"}'),
        (
            '{"url":"https:¦/¦/x.test¦/p","h":"Bearer abc¦/def¦/ghi¦/jklSECRETTAIL"}',
            '{"url":"https:¦/¦/x.test¦/p","h":"[REDACTED]"}',
        ),
    ],
)
def test_json_short_escape_of_a_slash_is_written_out_before_matching(body: str, expected: str) -> None:
    scrubbed, hits = scrub_network_body(_j(body), "application/json")

    assert scrubbed == _j(expected)
    assert json.loads(scrubbed)
    assert hits


def test_json_password_with_escaped_spaces_is_still_redacted() -> None:
    # Written out, the spaces would end the `password` match after two characters; the text as it
    # arrived is matched too, and the two results are combined.
    body = _j('{"password":"my¦u0020pass¦u0020word","n":1}')

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert scrubbed == '{"password":"[REDACTED]","n":1}'
    assert [hit["pattern"] for hit in hits] == ["password_field"]


@pytest.mark.parametrize(
    "value",
    ["hunter¦u0020two", "ab¦u0020cdefgh", "p¦u0026ssw0rd!", "hunter¦u0027s"],
    ids=["space", "short-then-space", "ampersand", "apostrophe"],
)
def test_json_password_with_an_escaped_name_and_an_escaped_terminator_in_its_value_is_redacted_whole(
    value: str,
) -> None:
    # The raw text cannot read the escaped name, and the fully written-out text ends the value at the
    # space, `&` or `'`; only a view with the name written out and these escapes left alone sees all of it.
    body = _j('{"pass¦u0077ord":"' + value + '"}')

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert scrubbed == _j('{"pass¦u0077ord":"[REDACTED]"}')
    assert {hit["pattern"] for hit in hits} == {"password_field"}


def _escape_every_character(members: dict[str, str]) -> str:
    """JSON text in which every character of every key and value is written as a u-escape."""

    def encode(text: str) -> str:
        return '"' + "".join(f"¦u{ord(char):04x}" for char in text) + '"'

    return _j("{" + ",".join(f"{encode(key)}:{encode(value)}" for key, value in members.items()) + "}")


@pytest.mark.parametrize("password", ["p&ssw0rd!", "correct horse battery", "hunter2xyz", "it's a secret"])
def test_json_password_is_redacted_whole_under_an_encoder_that_escapes_every_character(password: str) -> None:
    body = _escape_every_character({"password": password})
    assert json.loads(body) == {"password": password}

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert json.loads(scrubbed) == {"password": "[REDACTED]"}
    assert {hit["pattern"] for hit in hits} == {"password_field"}


@pytest.mark.parametrize(
    ("members", "secret"),
    [
        ({"email": "ops@example.com"}, "ops"),
        ({"card": "4111 1111 1111 1111"}, "4111"),
        ({"auth": "Bearer abc/def+ghi=="}, "abc"),
    ],
)
def test_json_values_stay_valid_json_under_an_encoder_that_escapes_every_character(
    members: dict[str, str], secret: str
) -> None:
    scrubbed, hits = scrub_network_body(_escape_every_character(members), "application/json")

    assert hits
    assert list(json.loads(scrubbed).values()) == ["[REDACTED]"]
    assert secret not in scrubbed


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        ('{"card":"4111 1111 1111 1111 12/27"}', "application/json", '{"card":"[REDACTED] 12/27"}'),
        (
            '{"card":"4111¦u00201111¦u00201111¦u00201111¦u002012/27"}',
            "application/json",
            '{"card":"[REDACTED]¦u002012/27"}',
        ),
        ("card=4111+1111+1111+1111+12%2F27", FORM, "card=%5BREDACTED%5D+12%2F27"),
        ("card=4111%201111%201111%201111%2012%2F27&n=1", FORM, "card=%5BREDACTED%5D%2012%2F27&n=1"),
    ],
)
def test_a_card_followed_by_its_expiry_is_redacted_in_json_and_forms(
    body: str, content_type: str, expected: str
) -> None:
    scrubbed, hits = scrub_network_body(_j(body), content_type)

    assert scrubbed == _j(expected)
    assert [hit["pattern"] for hit in hits] == ["credit_card"]


def test_json_match_that_starts_inside_an_escape_takes_the_whole_escape_and_stays_valid() -> None:
    # `u0020y@example.com` is what the raw pass sees as the email; the backslash before it belongs to it.
    body = _j('{"a":"x¦u0020y@example.com","b":"ops¦u0040example.com"}')

    scrubbed, _ = scrub_network_body(body, "application/json")

    assert json.loads(scrubbed) == {"a": "x[REDACTED]", "b": "[REDACTED]"}


def test_json_the_same_match_seen_by_both_views_is_reported_once() -> None:
    body = _j('{"a":"ops@example.com","b":"ops¦u0040example.com"}')

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert scrubbed == '{"a":"[REDACTED]","b":"[REDACTED]"}'
    assert [hit["pattern"] for hit in hits] == ["email", "email"]


@pytest.mark.parametrize(
    ("body", "pattern"),
    [
        # The text as it arrived ends the match at the escaped slash; the written-out text runs on past it.
        ('{"h":"Bearer abcdefgh¦u002fTAILSECRET"}', "bearer_token"),
        # The fully written-out text ends it at the space; the arrived text and the token view run on.
        ('{"password":"hunter¦u0020two"}', "password_field"),
        ('{"pass¦u0077ord":"hunter¦u0020two"}', "password_field"),
        (_escape_every_character({"password": "correct horse battery"}), "password_field"),
    ],
    ids=["escaped-slash", "escaped-space", "escaped-name", "every-character"],
)
def test_json_one_redaction_that_the_views_match_to_different_lengths_is_reported_once(body: str, pattern: str) -> None:
    scrubbed, hits = scrub_network_body(_j(body), "application/json")

    assert "TAILSECRET" not in scrubbed and "two" not in scrubbed and "battery" not in scrubbed
    assert [hit["pattern"] for hit in hits] == [pattern]


def test_json_patterns_that_overlap_on_one_stretch_are_each_reported_once() -> None:
    # bearer_token takes the whole value and jwt_token the token inside it: one redaction, two patterns.
    # Assembled from parts so secret scanners do not flag a synthetic fixture.
    jwt = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "abcdefghijk"])
    body = _j('{"h":"Bearer ' + jwt + '¦u002fx","e":"ops¦u0040example.com"}')

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert json.loads(scrubbed) == {"h": "[REDACTED]", "e": "[REDACTED]"}
    assert sorted(hit["pattern"] for hit in hits) == ["bearer_token", "email", "jwt_token"]


def test_json_with_only_escapes_that_stay_escaped_is_scrubbed_as_it_arrived() -> None:
    body = _j('{"note":"say ¦u0022hi¦u0022¦n","email":"ops@example.com"}')

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert scrubbed == _j('{"note":"say ¦u0022hi¦u0022¦n","email":"[REDACTED]"}')
    assert [hit["pattern"] for hit in hits] == ["email"]


JSON = "application/json"
JSON_SECRET_NAMES = [
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apiKey",
    "client_secret",
    "secret_key",
    "accessToken",
    "x-api-key",
    "token",
    "secret",
    "password_confirmation",
    "passwordConfirmation",
    "passphrase",
    "cvv",
    "pin",
]


@pytest.mark.parametrize("name", JSON_SECRET_NAMES)
def test_json_member_with_a_secret_name_loses_its_value_and_nothing_else(name: str) -> None:
    # No pattern matches `opaque...`, and `"name":` is not `name=`: the name is all that marks the value.
    body = json.dumps({"a": 1, name: "opaqueValue-1234567890", "z": "keep"})

    scrubbed, hits = scrub_network_body(body, JSON)

    assert json.loads(scrubbed) == {"a": 1, name: "[REDACTED]", "z": "keep"}
    assert scrubbed == body.replace("opaqueValue-1234567890", "[REDACTED]")
    assert [hit["pattern"] for hit in hits] == ["password_field"]
    assert set(hits[0]) == HIT_KEYS
    assert "opaque" not in json.dumps(hits)


def test_json_oauth_token_response_is_redacted_by_value_and_stays_valid_json() -> None:
    body = (
        '{"access_token":"ya29.a0AfH6SMBopaqueAccess","token_type":"Bearer","expires_in":3599,'
        '"refresh_token":"1//0gOpaqueRefreshToken","scope":"openid email",'
        '"id_token":"opaque-id-token-value","user":{"name":"Ada","client_secret":"s3cr3t-value-here"}}'
    )

    scrubbed, hits = scrub_network_body(body, JSON)

    redacted = json.loads(scrubbed)
    assert redacted["access_token"] == redacted["refresh_token"] == redacted["id_token"] == "[REDACTED]"
    assert redacted["user"] == {"name": "Ada", "client_secret": "[REDACTED]"}
    # `token_type` holds the word token, so it goes too (its value only); the rest is untouched.
    assert redacted["token_type"] == "[REDACTED]"
    assert (redacted["expires_in"], redacted["scope"]) == (3599, "openid email")
    for secret in ("opaqueAccess", "OpaqueRefresh", "opaque-id-token", "s3cr3t"):
        assert secret not in scrubbed
    assert {hit["pattern"] for hit in hits} == {"password_field"}


def test_json_member_name_written_with_escapes_is_still_a_secret_name() -> None:
    body = _j('{"access_t¦u006fken":"opaque¦u0020value","¦u0061pi_key":"opaque-two"}')

    scrubbed, hits = scrub_network_body(body, JSON)

    assert scrubbed == _j('{"access_t¦u006fken":"[REDACTED]","¦u0061pi_key":"[REDACTED]"}')
    assert len(hits) == 2


def test_json_secret_named_member_is_redacted_beside_a_match_of_another_view() -> None:
    body = _j('{"mail":"ops¦u0040example.com","access_token":"opaque-value","password":"hunter¦u0020two"}')

    scrubbed, hits = scrub_network_body(body, JSON)

    assert scrubbed == '{"mail":"[REDACTED]","access_token":"[REDACTED]","password":"[REDACTED]"}'
    assert sorted(hit["pattern"] for hit in hits) == ["email", "password_field", "password_field"]


def test_json_secret_named_member_whose_value_a_pattern_already_redacted_is_reported_once() -> None:
    body = '{"access_token":"Bearer abcdefghijklmnop","api_key":"ops@example.com"}'

    scrubbed, hits = scrub_network_body(body, JSON)

    assert scrubbed == '{"access_token":"[REDACTED]","api_key":"[REDACTED]"}'
    assert [hit["pattern"] for hit in hits] == ["bearer_token", "email"]


@pytest.mark.parametrize(
    "body",
    [
        '{"note":"access_token","other":"value"}',  # a value that spells a secret name is no name
        '{"tokens":["a","b"],"monkey":"opaque","keyboard":"opaque","spin":"opaque"}',
        '{"access_token":"","refresh_token":null,"id_token":false}',
        '{"access_token": {"opaque": "x"}, "api_key": ["a", "b"]}',
        '{"access_token":"[REDACTED]"}',
        '["access_token","opaque-value"]',
        '{"access_token" "opaque-value"}',
    ],
    ids=["value-not-name", "other-names", "no-string-value", "nested", "already-redacted", "array", "no-colon"],
)
def test_json_without_a_string_under_a_secret_name_comes_back_untouched(body: str) -> None:
    assert scrub_network_body(body, JSON) == (body, [])


@pytest.mark.parametrize(
    "value", ["x", "abc", 'ab¦"cdefgh', 'a¦"b'], ids=["one-character", "three-characters", "quote-after-two", "quote"]
)
def test_json_password_that_the_password_pattern_cannot_match_is_redacted_by_name(value: str) -> None:
    # `password_field` needs four characters before a quote or a space; the name does not.
    scrubbed, hits = scrub_network_body(_j('{"password":"' + value + '","n":1}'), JSON)

    assert json.loads(scrubbed) == {"password": "[REDACTED]", "n": 1}
    assert [hit["pattern"] for hit in hits] == ["password_field"]


def test_json_secret_named_member_is_redacted_in_javascript_and_with_odd_spacing() -> None:
    body = 'var r = {\n  "api_key"  :\n  "opaque-value-12345" , "n": 1 };'

    scrubbed, _ = scrub_network_body(body, "text/javascript")

    assert scrubbed == 'var r = {\n  "api_key"  :\n  "[REDACTED]" , "n": 1 };'


def test_json_member_scan_reads_one_very_long_string_in_one_cheap_pass() -> None:
    # One group repeated per character cost 0.25 s per megabyte on a string like this; the form in use 0.012 s.
    text = '{"access_token":"' + "a" * 8_000_000 + '","b":"' + "c" * 8_000_000

    started = time.perf_counter()
    found = json_secret_values(text, "[REDACTED]")
    elapsed = time.perf_counter() - started

    assert [(end - start) for start, end, _ in found] == [8_000_000]
    assert elapsed < 1, f"{elapsed:.1f}s"


def test_json_secret_named_member_in_a_truncated_body_is_redacted_to_the_end() -> None:
    scrubbed, hits = scrub_network_body('{"n":1,"access_token":"opaque-valu', JSON)

    assert scrubbed == '{"n":1,"access_token":"[REDACTED]'
    assert hits


def test_json_secret_named_member_needs_the_password_field_pattern_to_be_enabled() -> None:
    body = '{"access_token":"opaque-value"}'

    assert scrub_network_body(body, JSON, enabled_patterns={"email"}) == (body, [])
    assert scrub_network_body(body, JSON, replacement="***", enabled_patterns={"password_field"})[0] == (
        '{"access_token":"***"}'
    )


def test_json_valid_in_stays_valid_out_over_random_escape_mixes() -> None:
    pieces = [
        "¦u0040", "¦u0020", "¦u002f", "¦u002d", "¦u0026", "¦u0027", "¦n", '¦"', "¦¦", "¦u00e9",
        "¦ud83d¦ude00", "a", "b", " ", "x@y.zz", "ops@example.com", "ops¦u0040example.com", "12", "-",
    ]  # fmt: skip
    rng = random.Random(11)  # noqa: S311 (a seeded, repeatable corpus)
    checked = 0
    for _ in range(400):
        value = _j("".join(rng.choice(pieces) for _ in range(rng.randint(1, 14))))
        body = '{"k":"' + value + _j('","email":"ops¦u0040example.com","n":1}')
        try:
            json.loads(body)
        except ValueError:
            continue  # the mix itself is not valid JSON
        checked += 1

        scrubbed, _ = scrub_network_body(body, "application/json")

        result = json.loads(scrubbed)
        assert (result["email"], result["n"]) == ("[REDACTED]", 1), (body, scrubbed)
    assert checked > 100


def test_json_escapes_outside_a_token_are_left_alone_and_the_body_stays_json() -> None:
    body = r'{"note":"it\u0027s \u0026 co","email":"ops\u0040example.com"}'

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert "ops" not in scrubbed
    assert r"\u0027" in scrubbed and r"\u0026" in scrubbed
    assert json.loads(scrubbed) == {"note": "it's & co", "email": "[REDACTED]"}
    assert hits


def test_json_surrogate_pairs_stay_escaped_and_the_result_encodes() -> None:
    body = r'{"a":"\ud83d\ude00","email":"ops\u0040example.com"}'

    scrubbed, _ = scrub_network_body(body, "application/json")

    assert r"\ud83d\ude00" in scrubbed
    assert "ops" not in scrubbed
    scrubbed.encode("utf-8")
    assert json.loads(scrubbed)["a"] == "😀"


def test_json_body_with_escapes_but_no_pii_comes_back_untouched() -> None:
    body = r'{"name":"caf\u00e9"}'

    assert scrub_network_body(body, "application/json") == (body, [])


def test_non_json_types_are_not_unescaped() -> None:
    body = r"ops\u0040example.com"

    assert scrub_network_body(body, "text/plain") == (body, [])


def test_json_that_is_not_strict_json_is_still_scrubbed() -> None:
    body = r"{email: 'ops\u0040example.com', /* c */ trailing: 1,}"

    scrubbed, hits = scrub_network_body(body, "application/json")

    assert scrubbed == "{email: '[REDACTED]', /* c */ trailing: 1,}"
    assert hits


# ── Request URLs ───────────────────────────────────────────────────────────


def test_url_query_value_is_scrubbed_and_the_rest_is_untouched() -> None:
    scrubbed, hits = pii_scrub.scrub_url_query("https://x.test/signup?email=ops%40example.com")

    assert scrubbed == "https://x.test/signup?email=%5BREDACTED%5D"
    assert [hit["pattern"] for hit in hits] == ["email"]
    assert set(hits[0]) == HIT_KEYS


def test_url_without_pii_comes_back_as_the_same_string() -> None:
    for url in (
        "https://x.test/search?q=shoes&page=2&blank=",
        "https://x.test/a/b",
        "https://x.test/a?",
        "https://x.test/a?q=caf%C3%A9#top",
        "",
    ):
        assert pii_scrub.scrub_url_query(url) == (url, [])


def test_url_keeps_pair_order_blank_values_and_fragment() -> None:
    url = "https://x.test/p?a=1&email=ops%40example.com&blank=&b=2#section"

    scrubbed, hits = pii_scrub.scrub_url_query(url)

    assert scrubbed == "https://x.test/p?a=1&email=%5BREDACTED%5D&blank=&b=2#section"
    assert len(hits) == 1


def test_url_query_keys_are_scrubbed_too() -> None:
    scrubbed, hits = pii_scrub.scrub_url_query("https://x.test/p?ops%40example.com=1")

    assert "ops" not in scrubbed
    assert hits


@pytest.mark.parametrize(
    "url",
    [
        "https://x.test/cb?code=abcdefghijklmnop&state=1",
        "https://x.test/p?state=1&api_key=abcdefgh12345678",
        "https://x.test/p?access_token=abcdefgh12345678",
    ],
    ids=["oauth-code-first", "api-key", "access-token"],
)
def test_url_query_secrets_that_need_the_key_are_scrubbed(url: str) -> None:
    scrubbed, hits = pii_scrub.scrub_url_query(url)

    assert "abcdefgh" not in scrubbed
    assert scrubbed.startswith("https://x.test/")
    assert hits


def test_url_scrub_honours_replacement_and_enabled_patterns() -> None:
    url = "https://x.test/p?email=ops%40example.com&card=4111+1111+1111+1111"

    scrubbed, hits = pii_scrub.scrub_url_query(url, replacement="***", enabled_patterns={"credit_card"})

    assert scrubbed == "https://x.test/p?email=ops%40example.com&card=%2A%2A%2A"
    assert [hit["pattern"] for hit in hits] == ["credit_card"]


def test_scrubber_url_method_scrubs_unless_network_scrubbing_is_off() -> None:
    url = "https://x.test/signup?email=ops%40example.com"

    scrubbed, hits = PiiScrubber().url(url)
    assert "ops" not in scrubbed
    assert hits

    assert PiiScrubber(network_enabled=False).url(url) == (url, [])
    assert PiiScrubber(enabled=False).url(url) == (url, [])


# ── Hostile input ──────────────────────────────────────────────────────────


def _count_pattern_passes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every full run of the pattern set (one per call), however it is spelled."""
    calls: list[str] = []
    real_scrub_text, real_find_spans = pii_scrub.scrub_text, pii_scrub.find_pii_spans

    def scrub_text(*args, **kwargs):
        calls.append("scrub_text")
        return real_scrub_text(*args, **kwargs)

    def find_pii_spans(*args, **kwargs):
        calls.append("find_pii_spans")
        return real_find_spans(*args, **kwargs)

    monkeypatch.setattr(pii_scrub, "scrub_text", scrub_text)
    monkeypatch.setattr(pii_scrub, "find_pii_spans", find_pii_spans)
    return calls


@pytest.mark.parametrize(
    "pair",
    ["k{i}=v%41+{i}", "e{i}=ops%40example.com", "token{i}=a%2Bb", "{i}", "="],
    ids=["encoded-no-pii", "every-pair-hits", "secret-names", "key-only", "empty-pairs"],
)
def test_form_and_url_scrub_cost_does_not_grow_with_the_number_of_pairs(
    monkeypatch: pytest.MonkeyPatch, pair: str
) -> None:
    # The pattern set costs a regex run over the whole text, so the number of runs is the cost
    # model: a run per pair was 7x the single-pass cost on a 16 KB body of `=&`.
    calls = _count_pattern_passes(monkeypatch)

    def passes(pairs: int) -> int:
        body = "&".join(pair.format(i=i) for i in range(pairs)) + "&email=ops%40example.com&password%5B%5D=x%2By"
        calls.clear()
        scrub_network_body(body, FORM)
        pii_scrub.scrub_url_query("https://x.test/p?" + body)
        return len(calls)

    small, large = passes(4), passes(2000)

    assert large == small
    assert large <= 4  # per call: the raw pass and one pass over the decoded text


@pytest.mark.parametrize(
    "unit",
    ["a=b&", "a.a@", "%40", "\\u0040", "\\\\", "&&==", '"', '"\\', '\\"', '"api_key":"', '"a":"api_key":'],
)
def test_encoded_paths_stay_linear_on_hostile_input(unit: str) -> None:
    text = unit * (100_000 // len(unit))
    started = time.perf_counter()
    scrub_network_body(text, FORM)
    scrub_network_body(text, "application/json")
    pii_scrub.scrub_url_query("https://x.test/p?" + text)
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"{unit!r} x {len(text)} chars took {elapsed:.1f}s"
