"""JSON bodies with no escapes: every secret-named member goes, and the JSON stays JSON.

The member scan used to read `scrub_text`'s output for a body with no escape worth writing out. There
`password_field` (a value with a space, `&` or `'`) and `api_key_param` (at the end of a string) each
took an odd number of structural quotes with them, which flipped the quote parity the scan relies on:
the tail of the password stayed, and so did every later secret-named member. Both patterns, and
`azure_secret` and `generic_b64_secret`, also took the closing quote, so the stored body no longer parsed.
"""

from __future__ import annotations

import json
import random

import pytest

from app.pii_scrub import PiiScrubber, find_pii_spans, scrub_network_body

JSON = "application/json"
BS = "\\"


def _scrub(body: str, **kwargs) -> tuple[str, list[dict]]:
    scrubbed, hits = scrub_network_body(body, JSON, **kwargs)
    assert isinstance(scrubbed, str)
    return scrubbed, hits


# ── No escapes: the quote-parity flip ──────────────────────────────────────

PARITY_BODIES = {
    "password-with-space": (
        '{"password":"correct horse","client_secret":"cs-opaque-111"}',
        '{"password":"[REDACTED]","client_secret":"[REDACTED]"}',
    ),
    "devise-signup": (
        '{"user":{"email":"a@b.co","password":"correct horse battery","password_confirmation":"correct horse battery"}}',
        '{"user":{"email":"[REDACTED]","password":"[REDACTED]","password_confirmation":"[REDACTED]"}}',
    ),
    "password-with-ampersand": (
        '{"password":"p&ssw0rd!x","refresh_token":"rt-opaque-222"}',
        '{"password":"[REDACTED]","refresh_token":"[REDACTED]"}',
    ),
    "password-with-apostrophe": (
        '{"pwd":"hunter2\'s","access_token":"at-opaque-333"}',
        '{"pwd":"[REDACTED]","access_token":"[REDACTED]"}',
    ),
    "password-with-escaped-quote": (
        '{"password":"ab' + BS + '"cdefg","api_key":"ak-opaque-444"}',
        '{"password":"[REDACTED]","api_key":"[REDACTED]"}',
    ),
    "api-key-in-a-link-then-a-token": (
        '{"next":"https://api.x.test/v1/items?api_key=abcdefgh12","refresh_token":"rt-opaque-555"}',
        '{"next":"https://api.x.test/v1/items?api_key=[REDACTED]","refresh_token":"[REDACTED]"}',
    ),
    "access-token-in-a-link-then-two-secrets": (
        '{"cb":"https://app.test/cb?access_token=abcdefgh12","id_token_hint":"opaque-666","secret":"sec-777"}',
        '{"cb":"https://app.test/cb?access_token=[REDACTED]","id_token_hint":"[REDACTED]","secret":"[REDACTED]"}',
    ),
    "camel-case-password-keys": (
        '{"currentPassword":"old pass 111","newPassword":"NewPass-222","sessionToken":"Opaque-ST-444"}',
        '{"currentPassword":"[REDACTED]","newPassword":"[REDACTED]","sessionToken":"[REDACTED]"}',
    ),
    "short-password-then-a-token": (
        '{"password":"correct horse","client_secret":"x"}',
        '{"password":"[REDACTED]","client_secret":"[REDACTED]"}',
    ),
    "short-camel-case-values": (
        '{"currentPassword":"old pass","newPassword":"N","sessionToken":"T"}',
        '{"currentPassword":"[REDACTED]","newPassword":"[REDACTED]","sessionToken":"[REDACTED]"}',
    ),
}


@pytest.mark.parametrize(("body", "expected"), PARITY_BODIES.values(), ids=PARITY_BODIES.keys())
def test_json_with_no_escapes_redacts_each_secret_member_whole_whatever_precedes_it(body: str, expected: str) -> None:
    scrubbed, hits = _scrub(body)

    assert scrubbed == expected
    assert hits


@pytest.mark.parametrize(
    "scrubber", [PiiScrubber(), PiiScrubber(enabled_patterns={"password_field"})], ids=["all", "name"]
)
def test_the_scrubber_service_redacts_the_same_bodies(scrubber: PiiScrubber) -> None:
    body = '{"password":"correct horse","client_secret":"cs-opaque-111"}'

    scrubbed, _ = scrubber.network_body(body, JSON)

    assert "horse" not in scrubbed and "cs-opaque" not in scrubbed


# ── The same bodies, fuzzed ────────────────────────────────────────────────

SECRET_KEYS = [
    "password", "access_token", "client_secret", "api_key", "refresh_token", "password_confirmation",
    "passphrase", "secret", "token", "pin", "cvv", "apiKey", "newPassword", "pwd",
]  # fmt: skip
PLAIN_KEYS = ["email", "name", "note", "url", "next", "query", "id", "kind", "status", "msg"]
FILL = "abcdefghijklmnopqrstuvwxyz ABCXYZ0123456789-_./:;,!?&=+%#@'\"\\é€😀\t\n"


def _needle_in(value: str) -> str:
    """The needle a fuzzed secret value carries: `N` and seven letters of `QWXZJKV`, or an empty string."""
    for index in range(len(value) - 7):
        chunk = value[index : index + 8]
        if chunk[0] == "N" and all(char in "QWXZJKV" for char in chunk[1:]):
            return chunk
    return ""


def _fuzz_body(rng: random.Random) -> tuple[str, list[str]]:
    """A json.dumps body with secret-named members whose values hold a needle, and the needles."""
    members: dict[str, str] = {}
    for _ in range(rng.randint(1, 6)):
        if rng.random() < 0.5:
            needle = "N" + "".join(rng.choice("QWXZJKV") for _ in range(7))
            pre = "".join(rng.choice(FILL) for _ in range(rng.randint(0, 6)))
            post = "".join(rng.choice(FILL) for _ in range(rng.randint(0, 6)))
            members[rng.choice(SECRET_KEYS)] = pre + needle + post
        elif rng.random() < 0.2:
            query_name = rng.choice(["api_key", "access_token", "token", "q"])
            members[rng.choice(PLAIN_KEYS)] = f"https://x.test/p?{query_name}=abcdefgh{rng.randint(10, 99)}"
        else:
            members[rng.choice(PLAIN_KEYS)] = "".join(rng.choice(FILL) for _ in range(rng.randint(0, 12)))
    body = json.dumps(members, ensure_ascii=rng.random() < 0.5, separators=rng.choice([(",", ":"), (", ", ": ")]))
    # A later duplicate key overwrote an earlier one, so only the needles that survived are counted.
    needles = [_needle_in(value) for key, value in members.items() if key in SECRET_KEYS]
    return body, [needle for needle in needles if needle]


def test_fuzzed_json_bodies_leak_no_secret_member_and_stay_valid_json() -> None:
    rng = random.Random(1024)  # noqa: S311 (a seeded, repeatable corpus)
    scrubber = PiiScrubber.from_settings(type("S", (), {})())
    checked = 0
    for _ in range(600):
        body, needles = _fuzz_body(rng)
        scrubbed, _ = scrubber.network_body(body, JSON)

        leaked = [needle for needle in needles if needle in scrubbed]
        assert not leaked, (body, scrubbed, leaked)
        json.loads(scrubbed)
        checked += bool(needles)
    assert checked > 300


# ── Value only: the JSON around a redaction stays JSON ─────────────────────

HEX = "0123456789abcdef0123456789abcdef"
B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2"
HEX40 = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("pattern", "body", "expected"),
    [
        (
            "api_key_param",
            '{"next":"https://api.x.test/v1?api_key=abcdefgh12","n":1}',
            '{"next":"https://api.x.test/v1?api_key=[REDACTED]","n":1}',
        ),
        (
            "password_field",
            '{"note":"my password=hunter2xyz","n":1}',
            '{"note":"my password=[REDACTED]","n":1}',
        ),
        (
            "azure_secret",
            '{"cfg":"tenant_id=' + HEX + '","n":1}',
            '{"cfg":"tenant_id=[REDACTED]","n":1}',
        ),
        (
            "generic_b64_secret",
            '{"cfg":"token=' + B64 + '","n":1}',
            '{"cfg":"token=[REDACTED]","n":1}',
        ),
        (
            "gcp_sa_key",
            '{"private_key_id":"' + HEX40 + '","n":1}',
            '{"private_key_id":"[REDACTED]","n":1}',
        ),
    ],
    ids=["api_key_param", "password_field", "azure_secret", "generic_b64_secret", "gcp_sa_key"],
)
def test_a_pattern_that_takes_the_closing_quote_redacts_only_the_value_in_json(
    pattern: str, body: str, expected: str
) -> None:
    scrubbed, hits = _scrub(body, enabled_patterns={pattern})

    assert scrubbed == expected
    assert json.loads(scrubbed)["n"] == 1
    assert [hit["pattern"] for hit in hits] == [pattern]
    # The match is now the value itself, so the hit must not preview it.
    assert hits[0]["preview"] == "…"


def test_a_value_pattern_in_json_hit_is_positioned_at_the_value() -> None:
    body = '{"note":"my password=hunter2xyz"}'

    _, hits = _scrub(body, enabled_patterns={"password_field"})

    assert body[hits[0]["offset"] : hits[0]["offset"] + hits[0]["length"]] == "hunter2xyz"


def test_value_only_redaction_applies_to_json_and_not_to_plain_text() -> None:
    text = 'note: "my password=hunter2xyz"'

    plain, _ = scrub_network_body(text, "text/plain", enabled_patterns={"password_field"})

    assert plain == 'note: "my [REDACTED]'


@pytest.mark.parametrize(
    "body",
    [
        '{"password":"hunter2xyz","n":1}',
        '{"pass":"abcd efgh","n":1}',
        '{"user":{"password":"correct horse","n":1}}',
        '{"items":[{"api_key":"abcdefghijk"},{"token":"x"}],"n":1}',
    ],
)
def test_json_keeps_parsing_after_every_password_and_key_redaction(body: str) -> None:
    scrubbed, hits = _scrub(body)

    assert hits
    json.loads(scrubbed)


def test_a_password_pattern_hit_on_an_escaped_name_keeps_the_name_and_the_json() -> None:
    body = '{"pass' + BS + 'u0077ord":"hunter2xyz","n":1}'

    scrubbed, hits = _scrub(body)

    assert scrubbed == '{"pass' + BS + 'u0077ord":"[REDACTED]","n":1}'
    assert json.loads(scrubbed)["n"] == 1
    assert {hit["pattern"] for hit in hits} == {"password_field"}


def test_a_gcp_service_account_key_stays_valid_json_and_loses_its_key_id_and_private_key() -> None:
    key = {
        "type": "service_account",
        "project_id": "p",
        "private_key_id": HEX40,
        # Assembled from parts so secret scanners do not flag a synthetic fixture.
        "private_key": "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END " + "PRIVATE KEY-----\n",
        "client_email": "svc@p.iam.gserviceaccount.com",
    }

    for body in (json.dumps(key), json.dumps(key, separators=(",", ":")), json.dumps(key, ensure_ascii=False)):
        scrubbed, hits = _scrub(body)

        parsed = json.loads(scrubbed)
        assert parsed["type"] == "service_account"
        assert parsed["private_key_id"] == "[REDACTED]"
        assert parsed["private_key"] == "[REDACTED]"
        assert HEX40 not in scrubbed and "MIIEvQIBADANBgkq" not in scrubbed
        assert "gcp_sa_key" in {hit["pattern"] for hit in hits}


# ── The word `pass` is a secret name ───────────────────────────────────────

PASS_NAMES = ["pass", "db_pass", "smtp_pass", "PASS", "wifiPass", "user_pass"]
# A value with a space is cut at the space by the password pattern; the rest is a word it does not know.
PASS_VALUES = ["correct horse battery", "p&ssword9", " hunter2xyz", "it's-secret-1", "ab", 'say "hi" now', "tab\there"]


@pytest.mark.parametrize("value", PASS_VALUES)
@pytest.mark.parametrize("name", PASS_NAMES)
def test_a_json_member_named_with_the_word_pass_is_redacted_whole_and_stays_json(name: str, value: str) -> None:
    for body in (json.dumps({name: value, "n": 1}), json.dumps({name: value, "n": 1}, separators=(",", ":"))):
        scrubbed, hits = _scrub(body)

        assert json.loads(scrubbed) == {name: "[REDACTED]", "n": 1}, (body, scrubbed)
        assert hits


def test_the_name_rule_does_not_read_bypass_or_passed_as_the_word_pass() -> None:
    # The password pattern, which has no word boundary, still takes a value of four characters or more
    # after `bypass`; a value it cannot match (short, or with a space) shows the name rule leaves it alone.
    body = json.dumps({"bypass": "ab cd", "passed": "x y z", "n": 1})

    scrubbed, hits = _scrub(body)

    assert scrubbed == body
    assert hits == []


def test_a_form_field_named_with_the_word_pass_loses_its_whole_value() -> None:
    body = "db_pass=ab&smtp_pass=correct+horse&wifiPass=p%26ssword9&bypass=ab&n=1"

    scrubbed, _ = scrub_network_body(body, "application/x-www-form-urlencoded")

    assert scrubbed == "db_pass=%5BREDACTED%5D&smtp_pass=%5BREDACTED%5D&wifiPass=%5BREDACTED%5D&bypass=ab&n=1"


# ── A value-only match found only in a decoded view has no preview ─────────

# The raw text has a backslash where the secret begins, so no pattern matches it as it arrived; the
# match exists only once the escape is written out, and a preview of it would show the start of the secret.
ESCAPED_VALUE_ONLY = {
    "azure_secret": '{"note":"client_secret=' + BS + 'u0061bcdef0123456789abcdef0123456789"}',
    "azure_secret-tenant": '{"note":"tenant_id: ' + BS + "u0061" + "b" * 35 + '"}',
    "generic_b64_secret": '{"note":"token=' + BS + "u0051" + "Q" * 33 + '"}',
    "gcp_sa_key": '{"private_key_id":"' + BS + "u0061" + "b" * 39 + '"}',
}


@pytest.mark.parametrize(("pattern", "body"), ESCAPED_VALUE_ONLY.items(), ids=ESCAPED_VALUE_ONLY.keys())
def test_a_value_only_match_that_exists_only_in_a_decoded_view_is_not_previewed(pattern: str, body: str) -> None:
    pattern = pattern.split("-")[0]

    assert find_pii_spans(body, {pattern}, json=True) == []  # matched as it arrived, it holds nothing
    scrubbed, hits = _scrub(body, enabled_patterns={pattern})

    assert [hit["pattern"] for hit in hits] == [pattern]
    assert hits[0]["preview"] == "…"
    assert "bcdef0123456789" not in scrubbed and "QQQQQQQQ" not in scrubbed and "bbbbbbbb" not in scrubbed
