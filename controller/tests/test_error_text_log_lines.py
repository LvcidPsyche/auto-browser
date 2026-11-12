"""A log line makes an error's text inside the guard that quotes and cuts it, so a text that cannot be made is named.

A line written as quoted_for_log(str(exc)) made the text before the helper's own guard: an error whose text cannot be
made (str(KeyError(x)) is repr(x), and an object's repr can raise) raised out of the except it was logged in. As a
%s argument, logging made it lazily and reported a failed format on stderr; error_text_for_log makes it in its own try.
"""

from __future__ import annotations

import ast
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError

from app.browser import prefetch
from app.browser.services.sessions import BrowserSessionService
from app.startup import extensions

_APP = Path(__file__).resolve().parents[1] / "app"
# A line break, an ESC and 5,000 characters, after a line a reader of the log would take for the controller's.
FORGED = "p\nWARNING:app.main:forged by a page\x1b[2J" + "x" * 5000


def _written(records: list[logging.LogRecord]) -> list[str]:
    """Each record as a handler writes it."""
    return [logging.Formatter().format(record) for record in records]


class _Unprintable:
    def __repr__(self) -> str:
        raise RuntimeError("no text for this one")


def _error_with_no_text() -> KeyError:
    """An error whose text cannot be made: a KeyError's text is its key's repr."""
    return KeyError(_Unprintable())


class AnErrorWithNoTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_teardown_step_that_failed_so_is_recorded_and_the_steps_after_it_run(self) -> None:
        # The step's log line raised out of its except: the step was not recorded as failed, and the close of the
        # context, the step after it, never ran.
        context = SimpleNamespace(close=AsyncMock())
        session = SimpleNamespace(
            id="s1", network_inspector=None, context=context, browser=None, owns_browser=False, runtime=None
        )
        manager = SimpleNamespace(
            observation=SimpleNamespace(stop_trace_recording=AsyncMock(side_effect=_error_with_no_text())),
            window_births=SimpleNamespace(forget_session=Mock()),
        )
        failed: list[str] = []

        with self.assertLogs("app.browser.services.sessions", "WARNING") as logged:
            await BrowserSessionService(manager)._release_resources(session, failed)

        self.assertEqual(failed, ["stop trace recording"])
        context.close.assert_awaited_once()
        self.assertEqual(
            logged.output,
            [
                "WARNING:app.browser.services.sessions:failed to stop trace recording for session s1: "
                "<its text could not be made: RuntimeError>"
            ],
        )

    async def test_a_failed_attach_so_is_logged_and_the_session_created_hook_returns(self) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                browser_manager=None,
                network_inspectors={},
                cdp_sessions={},
                settings=SimpleNamespace(stealth_enabled=True, stealth_profile="light"),
            )
        )
        with (
            patch("app.cdp.passthrough.CDPPassthrough.from_page", new=AsyncMock(side_effect=_error_with_no_text())),
            patch("app.stealth.fingerprint.apply_fingerprint", new=AsyncMock()) as apply,
            self.assertLogs("app.startup.extensions", "WARNING") as logged,
        ):
            await extensions.on_session_created(app, "s1", SimpleNamespace(context=object()))

        # The step after it ran.
        apply.assert_awaited_once()
        self.assertEqual(
            logged.output,
            [
                "WARNING:app.startup.extensions:on_session_created: cdp session failed — "
                "<its text could not be made: RuntimeError>"
            ],
        )


def _makes_a_text(argument: ast.expr) -> bool:
    """Whether ``argument`` makes a text before the call it is passed to runs: str(x), an f-string, x.message."""
    if isinstance(argument, ast.JoinedStr):
        return True
    if isinstance(argument, ast.Attribute):
        return argument.attr == "message"
    return isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name) and argument.func.id == "str"


class NoTextMadeOutsideTheGuardTests(unittest.TestCase):
    def test_quoted_for_log_is_never_passed_a_text_made_for_it(self) -> None:
        # Such a text is an error's (str(exc), exc.message, str(task.exception())): error_text_for_log makes it.
        made: list[str] = []
        for path in sorted(_APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "quoted_for_log" and _makes_a_text(node.args[0]):
                    made.append(f"{path.relative_to(_APP).as_posix()}:{node.lineno}")
        self.assertEqual(made, [], "pass the error to error_text_for_log, which makes its text in its own try")


class CutLinesAreQuotedTests(unittest.IsolatedAsyncioTestCase):
    """Lines that wrote an error's text cut by slicing (f"{type(exc).__name__}: {exc}"[:200]): bounded, but a line
    break in it began a line of the log and an ESC reached the terminal. Each now writes it quoted and cut."""

    def assert_one_line(self, written: str, starts: str) -> None:
        self.assertTrue(written.startswith(starts), ascii(written[:120]))
        self.assertEqual(written.splitlines(), [written], ascii(written[:300]))
        self.assertNotIn("\x1b", written)
        self.assertIn("forged by a page\\x1b[2J", written)
        self.assertLess(len(written), 600, ascii(written[:300]))

    async def test_the_line_of_an_isolated_browser_that_did_not_close(self) -> None:
        manager = SimpleNamespace(request_gates=SimpleNamespace(ending=Mock()))
        browser = SimpleNamespace(close=AsyncMock(side_effect=PlaywrightError(FORGED)))

        with self.assertLogs("app.browser.services.sessions", "ERROR") as logged:
            self.assertFalse(await BrowserSessionService(manager)._close_owned_browser(browser, "s1"))

        (written,) = _written(logged.records)
        self.assert_one_line(written, "the isolated browser of session s1 did not close (Error: 'p\\nWARNING:")

    async def test_the_line_of_a_session_that_could_not_be_recorded_as_closed(self) -> None:
        manager = SimpleNamespace(session_store=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError(FORGED))))

        with self.assertLogs("app.browser.services.sessions", "ERROR") as logged:
            await BrowserSessionService(manager)._persist_closed_without_summary(SimpleNamespace(id="s1"), None)

        (written,) = _written(logged.records)
        self.assert_one_line(written, "could not record session s1 as closed: RuntimeError: 'p\\nWARNING:")

    async def test_the_line_of_a_prefetch_cleanup_that_failed(self) -> None:
        async def detach() -> None:
            raise RuntimeError(FORGED)

        with self.assertLogs("app.browser.prefetch", "WARNING") as logged:
            await prefetch._bounded_cleanup(detach(), "detach its browser session")

        (written,) = _written(logged.records)
        self.assert_one_line(
            written, "prefetch preference: could not detach its browser session: RuntimeError: 'p\\nWARNING:"
        )


_LOG_CALLS = frozenset({"debug", "info", "warning", "error", "critical", "exception", "log"})


def _a_text_made_for_the_line(argument: ast.expr) -> bool:
    """Whether ``argument`` is a text made before logging takes it (an f-string, str(x), repr(x)), or one cut."""
    if isinstance(argument, ast.Subscript) and isinstance(argument.slice, ast.Slice):
        argument = argument.value
    if isinstance(argument, ast.JoinedStr):
        return True
    return (
        isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name) and argument.func.id in {"str", "repr"}
    )


class NoLogArgumentMadeForTheLineTests(unittest.TestCase):
    def test_no_log_call_is_passed_a_text_made_or_cut_for_it(self) -> None:
        # Made before the call, an error's text is made outside any guard, and cut by slicing it is neither quoted
        # nor escaped: error_text_for_log does both (quoted_for_log for a text that is not an error's).
        made: list[str] = []
        for path in sorted(_APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in _LOG_CALLS or "log" not in ast.unparse(node.func.value).lower():
                    continue
                arguments = node.args[2:] if node.func.attr == "log" else node.args[1:]
                made += [
                    f"{path.relative_to(_APP).as_posix()}:{argument.lineno}"
                    for argument in arguments
                    if _a_text_made_for_the_line(argument)
                ]
        self.assertEqual(made, [], "pass the error to error_text_for_log, and any other text to quoted_for_log")


if __name__ == "__main__":
    unittest.main()
