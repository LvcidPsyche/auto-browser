"""A log line makes an error's text inside the guard that quotes and cuts it, so a text that cannot be made is named.

A line written as quoted_for_log(str(exc)) made the text before the helper's own guard: an error whose text cannot be
made (str(KeyError(x)) is repr(x), and an object's repr can raise) raised out of the except it was logged in. As a
%s argument, logging made it lazily and reported a failed format on stderr; error_text_for_log makes it in its own try.
"""

from __future__ import annotations

import ast
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError

from app.browser import prefetch
from app.browser.services.sessions import BrowserSessionService
from app.session_store import FileSessionStore
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


# A parameter by one of these names, or annotated with a class whose name ends so, holds an error.
_ERROR_PARAMETERS = frozenset({"exc", "error", "err", "cause", "failure"})
_ERROR_CLASS_ENDINGS = ("Exception", "Error")


def _error_names(scope: ast.AST) -> set[str]:
    """The names that hold an error in ``scope``: an except clause's own, or a function's parameters that do."""
    if isinstance(scope, ast.ExceptHandler):
        return {scope.name} if scope.name else set()
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        return set()
    names: set[str] = set()
    for parameter in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]:
        annotation = ast.unparse(parameter.annotation).replace('"', "") if parameter.annotation else ""
        classes = [part.strip() for part in annotation.split("|")]
        if parameter.arg in _ERROR_PARAMETERS or any(name.endswith(_ERROR_CLASS_ENDINGS) for name in classes):
            names.add(parameter.arg)
    return names


def _an_error_passed_whole(argument: ast.expr, errors: set[str]) -> bool:
    """Whether a log argument is an error itself, which logging prints as it is: one of ``errors``, its .message or
    its .args (or one of them), or a task's .exception()."""
    if isinstance(argument, ast.Subscript):
        argument = argument.value
    if isinstance(argument, ast.Attribute) and argument.attr in {"message", "args"}:
        argument = argument.value
    if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Attribute):
        return argument.func.attr == "exception" and not argument.args
    return isinstance(argument, ast.Name) and argument.id in errors


def _log_arguments(node: ast.AST) -> list[ast.expr] | None:
    """A log call's arguments after its message, or None for a call that is not a log call."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    if node.func.attr not in _LOG_CALLS or "log" not in ast.unparse(node.func.value).lower():
        return None
    return node.args[2:] if node.func.attr == "log" else node.args[1:]


def errors_passed_whole(tree: ast.AST) -> list[int]:
    """The lines of each log call in ``tree`` that is passed an error whole (see _an_error_passed_whole)."""
    lines: list[int] = []

    def visit(node: ast.AST, errors: set[str]) -> None:
        arguments = _log_arguments(node)
        if arguments is not None and any(_an_error_passed_whole(argument, errors) for argument in arguments):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, errors | _error_names(child))

    visit(tree, set())
    return sorted(set(lines))


class NoErrorPassedWholeToALogCallTests(unittest.TestCase):
    def test_no_log_call_is_passed_an_error_whole(self) -> None:
        # logging writes such an error's text as it is: unquoted, uncut, and made outside any guard. A line break in
        # it begins a line of the log, and a pydantic ValidationError prints the values it refused.
        passed: list[str] = []
        for path in sorted(_APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            passed += [f"{path.relative_to(_APP).as_posix()}:{line}" for line in errors_passed_whole(tree)]
        self.assertEqual(passed, [], "pass the error to error_text_for_log")


class AValidationErrorLoggedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stored_session_record_that_does_not_validate_is_one_line_without_its_values(self) -> None:
        # pydantic's text of the error is several lines, and repeats each value it refused: here a page's title.
        record = {
            "id": "s1",
            "name": "s1",
            "created_at": "2026-11-12T00:00:00Z",
            "updated_at": "2026-11-12T00:00:00Z",
            "status": "the page's own\nWARNING:app.main:forged by a page\x1b[2J",
            "current_url": "https://example.test/",
            "title": ["a page title\nWARNING:app.main:forged by a page"],
            "artifact_dir": "a",
            "takeover_url": "t",
            "remote_access": {},
        }
        with tempfile.TemporaryDirectory() as root:
            Path(root, "s1.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertLogs("app.session_store", "WARNING") as logged:
                self.assertEqual(await FileSessionStore(root).list(), [])

        (written,) = _written(logged.records)
        self.assertEqual(written.splitlines(), [written], ascii(written[:300]))
        self.assertTrue(
            written.endswith("s1.json: '2 refused by SessionRecord: status: literal_error; title: string_type'"),
            ascii(written),
        )
        self.assertNotIn("forged", written)


if __name__ == "__main__":
    unittest.main()
