"""
pii_secret_fields.py — values that are secrets because of the name they sit under, not how they look.

`user[password]=hunter2xyz` matches no value pattern: the name is what says the value is a secret. This
module holds the rule that reads a name and finds such values in a form (`key=value&...`), as spans of
the text. Stdlib only: the patterns live in pii_scrub.
"""

from __future__ import annotations

import functools
import re
import urllib.parse

from .pii_views import Found, decode_json_escapes

# A word in a name that makes the field a secret: `new_password`, `accessToken` and `client-secret` each
# hold one. `tokens` and `tokenizer_name` hold none, and `secretary` is not `secret`.
_SECRET_WORDS = frozenset({"password", "passwd", "passphrase", "pwd", "pass", "secret", "token", "apikey"})

# Names that are a secret only as a whole segment: `pin` is also in `spin` and `pinned`.
_SECRET_SEGMENTS = frozenset({"cvv", "cvc", "csc", "pin"})

# A name that holds one of these is a password whatever is written around it: `password2`, `mypassword`.
_PASSWORD_STEMS = ("password", "passwd", "passphrase")

# A name is read segment by segment: `user[password][new]` and `auth.token` have two each, and the index
# of `password[0]` is no segment at all. Words are split on case and digits as well as punctuation.
_SEGMENTS = re.compile(r"[\[\].]")
_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORDS = re.compile(r"[a-z]+|[0-9]+")


# Two words in a row that end in a key and are not secret: `public_key`, `publishable_key` and `site_key`
# are handed to every visitor. Only the pair is exempt, so `public_key_secret` still holds a word that counts.
_NOT_SECRET_PAIRS = frozenset({("public", "key"), ("publishable", "key"), ("site", "key")})

# `next_page_token` is a cursor into a list. Only the run is exempt, so `page_access_token` still holds a word
# that counts, and `page_token` alone is not a cursor (a Facebook page access token is one): a cursor needs
# the direction in front of it.
_CURSOR_DIRECTIONS = frozenset({"next", "prev", "previous"})


def _word_is_secret(words: list[str], index: int) -> bool:
    """Whether the word at `index` counts: a secret word, unless it ends a paging cursor."""
    if words[index] not in _SECRET_WORDS:
        return False
    is_cursor = index >= 2 and words[index] == "token" and words[index - 1] == "page"
    return not (is_cursor and words[index - 2] in _CURSOR_DIRECTIONS)


def _segment_names_a_secret(segment: str) -> bool:
    lowered = segment.lower()
    if lowered in _SECRET_SEGMENTS or any(stem in lowered for stem in _PASSWORD_STEMS):
        return True
    words = _WORDS.findall(_CASE_BOUNDARY.sub("_", segment).lower())
    if any(_word_is_secret(words, index) for index in range(len(words))):
        return True
    # `api_key` and `apiKey` end in a key; the word alone (`key`, `monkey`, `keyboard`) is not a secret.
    return len(words) > 1 and words[-1] == "key" and (words[-2], "key") not in _NOT_SECRET_PAIRS


def _names_a_secret(key: str) -> bool:
    return any(
        _segment_names_a_secret(segment) for segment in _SEGMENTS.split(key) if segment and not segment.isdigit()
    )


# A page chooses the names in its own traffic, and every name cached stays in memory: 1024 names of the
# 16 KB a body may hold were 17 MB, and a raised NETWORK_INSPECTOR_BODY_MAX_BYTES multiplies that. Only a
# name of ordinary length is cached; a longer one is read each time.
_MAX_CACHED_NAME_LENGTH = 256
_cached_names_a_secret = functools.lru_cache(maxsize=1024)(_names_a_secret)


def names_a_secret(key: str) -> bool:
    """Whether a decoded parameter or field name says its value is a secret, in any of its segments."""
    if len(key) > _MAX_CACHED_NAME_LENGTH:
        return _names_a_secret(key)
    return _cached_names_a_secret(key)


# Any name `names_a_secret` accepts contains one of these, so text without them has none to look for.
SECRET_NAME_HINT = re.compile(r"(?i)pass|pwd|secret|token|key|pin|cvv|cvc|csc")


def _secret_found(start: int, end: int) -> Found:
    # No preview: what follows the name is the secret itself.
    return start, end, {"pattern": "password_field", "offset": start, "length": end - start, "preview": "…"}


def form_secret_values(text: str, replacement: str) -> list[Found]:
    """The value of every parameter in `key=value&...` whose name says it is a secret, as spans of `text`."""
    spent = {replacement, urllib.parse.quote_plus(replacement)}
    found: list[Found] = []
    position = 0
    for part in text.split("&"):
        key, equals, value = part.partition("=")
        if equals and value and value not in spent:
            name = urllib.parse.unquote_plus(key, errors="replace") if "%" in key or "+" in key else key
            if SECRET_NAME_HINT.search(name) and names_a_secret(name):
                start = position + len(key) + 1
                found.append(_secret_found(start, start + len(value)))
        position += len(part) + 1
    return found


# A JSON string (one with no closing quote runs to the end of the text, so a cut-off body is still read),
# or else everything up to the next string. Every match consumes input, so reading the text is linear
# however it is built; a pattern for a whole `"name": "value"` member retries from every quote. The string
# is written as runs of plain characters between escapes, with possessive loops that keep no backtracking
# state: one group repeated per character was 20 times slower on a long string.
_JSON_TOKEN = re.compile(r'"[^"\\]*+(?:\\[\s\S]?[^"\\]*+)*+("|\Z)|[^"]+')
_COLON = re.compile(r"\s*:\s*")


def _json_string_names_a_secret(string: re.Match[str]) -> bool:
    name = string.group()[1:-1]
    if "\\" in name:
        name = decode_json_escapes(name).text
    return bool(SECRET_NAME_HINT.search(name)) and names_a_secret(name)


def json_secret_values(text: str, replacement: str) -> list[Found]:
    """The content of each string in JSON `text` that is the value of a member whose name says it is a secret.

    The text is read as strings and the gaps between them: a string, then a colon, then a string is a
    member. A name written with escapes (`access_t\\u006fken`) is read with them written out. Only the
    inside of the value string is returned, so the quotes, the name and the commas stay and JSON stays
    JSON; a value that is not a string (a number, an array, an object) is left alone.
    """
    found: list[Found] = []
    last = armed = None  # the string just read, and that string when only a colon has followed it
    for token in _JSON_TOKEN.finditer(text):
        if token.group()[0] != '"':
            armed = last if last is not None and _COLON.fullmatch(token.group()) else None
            last = None
            continue
        if armed is not None and _json_string_names_a_secret(armed):
            start, end = token.start() + 1, token.end() - len(token.group(1))
            if end > start and text[start:end] != replacement:
                found.append(_secret_found(start, end))
        last, armed = token, None
    return found
