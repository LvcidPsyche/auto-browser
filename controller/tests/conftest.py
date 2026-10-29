from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_asyncio

from tests import _background

# PRIVATE pytest-asyncio API: the name of its fixture for the event loop of each loop scope
# (pytest_asyncio/plugin.py, _create_scoped_runner_fixture). Nothing public gives a fixture the test's
# loop. requirements-dev.txt caps pytest-asyncio below its next minor release for this; a release that
# renames it fails every asyncio test here by this name, not silently.
_RUNNER_FIXTURE = "_{scope}_scoped_runner"


def leaves_no_background_tasks(request: pytest.FixtureRequest) -> Iterator[None]:
    """Every test waits only for the background tasks it started (tests/_background.py), and leaves none
    behind: what is still pending _background.SETTLE_SECONDS after it is cancelled and fails it by name,
    and so does a task of its that failed. The autouse fixture below; a plain generator, so a test can
    drive it (tests/test_background_settle.py)."""
    _hold_the_tests_loop(request)
    _background.mark()
    yield
    left = _background.settle(_background.SETTLE_SECONDS)
    if left:
        pytest.fail(
            f"background tasks the test left still pending {_background.SETTLE_SECONDS:g} s after it, or "
            f"that failed: {', '.join(left)}"
        )


_mark_background_tasks = pytest.fixture(autouse=True, name="_mark_background_tasks")(leaves_no_background_tasks)


def _hold_the_tests_loop(request: pytest.FixtureRequest) -> None:
    """Set an asyncio test's event loop up before this fixture, so it is torn down after it, still open.

    pytest-asyncio adds the loop's fixture after the autouse ones, so it was torn down first, and closing
    the loop cancelled what the test left pending before anything could see it.
    """
    if not pytest_asyncio.is_async_test(request.node):
        return
    marker = request.node.get_closest_marker("asyncio")
    scope = (marker.kwargs.get("loop_scope") if marker is not None else None) or request.config.getini(
        "asyncio_default_test_loop_scope"
    )
    name = _RUNNER_FIXTURE.format(scope=scope)
    try:
        request.getfixturevalue(name)
    except pytest.FixtureLookupError:
        pytest.fail(
            f"pytest-asyncio has no fixture {name!r} (a private name tests/conftest.py relies on, see "
            "_RUNNER_FIXTURE): find the fixture that now runs the test's event loop before lifting the cap",
            pytrace=False,
        )
