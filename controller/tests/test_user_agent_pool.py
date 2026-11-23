"""USER_AGENT_POOL entries reach a stealth context whole, and the default pool claims the engine's Chrome version.

Every Chrome UA contains "(KHTML, like Gecko)", so the pool, joined and split on commas, handed contexts a fragment
such as "like Gecko) Chrome/149.0.0.0 Safari/537.36". Entries are separated by "|" or newlines.

The default pool names the engine's version by hand, and a user agent of another Chrome than the engine is itself a
signal; a fix on 2026-11-03 moved it from 149 to 156 after the engine had moved.
"""

from __future__ import annotations

import re

import pytest

from app.config import Settings
from app.stealth.fingerprint import CHROME_UA_POOL
from tests._chromium import driver_chromium

_CHROME_MAJOR = re.compile(r"\bChrome/(\d+)\.")
_EDGE_MAJOR = re.compile(r"\bEdg/(\d+)\.")


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


def test_the_default_pool_follows_the_chromium_the_installed_driver_names() -> None:
    """The newest Chrome in the pool is the engine's major (the Chromium the installed Playwright driver names, as
    browser-node runs the same build: scripts/check_playwright_pins.py --installed), none is more than one major
    behind it, and an Edge entry's version is its Chrome's. No browser needed."""
    engine = int(driver_chromium()["chromium"]["browserVersion"].split(".")[0])
    chrome = {agent: _CHROME_MAJOR.search(agent) for agent in CHROME_UA_POOL}
    assert all(chrome.values()), [agent for agent, found in chrome.items() if not found]
    majors = {agent: int(found.group(1)) for agent, found in chrome.items()}

    problems = []
    if max(majors.values()) != engine:
        problems.append(f"the newest Chrome in the pool is {max(majors.values())}, the engine is {engine}")
    problems += [
        f"{major} is more than one major behind {engine}: {agent}"
        for agent, major in majors.items()
        if major < engine - 1
    ]
    for agent, major in majors.items():
        edge = _EDGE_MAJOR.search(agent)
        if edge and int(edge.group(1)) != major:
            problems.append(f"Edge {edge.group(1)} on Chrome {major}: {agent}")

    assert problems == []
