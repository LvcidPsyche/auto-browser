"""USER_AGENT_POOL entries reach a stealth context whole.

Every Chrome UA contains "(KHTML, like Gecko)", so the pool, joined and split on commas, handed contexts a fragment
such as "like Gecko) Chrome/149.0.0.0 Safari/537.36". Entries are separated by "|" or newlines.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.stealth.fingerprint import CHROME_UA_POOL


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_every_default_pool_entry_is_a_whole_user_agent() -> None:
    settings = _settings()
    for _ in range(50):
        assert settings.random_user_agent in CHROME_UA_POOL


@pytest.mark.parametrize("separator", ["|", "\n", " | "])
def test_an_operators_pool_splits_on_bars_or_newlines(separator: str) -> None:
    first, second = CHROME_UA_POOL[0], CHROME_UA_POOL[1]
    settings = _settings(USER_AGENT_POOL=separator.join([first, second]))

    assert {settings.random_user_agent for _ in range(50)} <= {first, second}


def test_a_single_user_agent_with_commas_is_kept_whole() -> None:
    settings = _settings(USER_AGENT_POOL=CHROME_UA_POOL[0])

    assert settings.random_user_agent == CHROME_UA_POOL[0]


def test_an_empty_pool_gives_no_user_agent() -> None:
    assert _settings(USER_AGENT_POOL=" | ").random_user_agent == ""
