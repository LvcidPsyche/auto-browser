"""A log line makes an error's text inside the guard that quotes and cuts it, so a text that cannot be made is named.

A line written as quoted_for_log(str(exc)) made the text before the helper's own guard: an error whose text cannot be
made (str(KeyError(x)) is repr(x), and an object's repr can raise) raised out of the except it was logged in. As a
%s argument, logging made it lazily and reported a failed format on stderr; error_text_for_log makes it in its own try.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ValidationError

from app.action_errors import BrowserUnreachableError
from app.browser import prefetch
from app.browser.request_gate import RequestGate
from app.browser.services import runtime
from app.browser.services.sessions import BrowserSessionService
from app.session_store import FileSessionStore
from app.startup import extensions
from app.utils import MAX_LOGGED_PATH_CHARS, error_text

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

    async def test_a_gate_session_that_did_not_detach_so_is_logged_and_the_detach_returns(self) -> None:
        # Whether the session went with its target was read from the error's text, made before the guarded line:
        # the detach raised, and its callers (a close, a failed attach) met an error of the text instead.
        gate = RequestGate(
            object(), url_refused=lambda url: False, attach_mode=False, origin_switch_declared=False, on_lost=Mock()
        )
        gate._cdp = SimpleNamespace(detach=AsyncMock(side_effect=_error_with_no_text()))

        with self.assertLogs("app.browser.request_gate", "WARNING") as logged:
            await gate.detach()

        self.assertEqual(
            logged.output,
            [
                "WARNING:app.browser.request_gate:pre-request gate: could not detach its session: KeyError: "
                "<its text could not be made: RuntimeError>"
            ],
        )

    async def test_a_cdp_connect_that_failed_so_is_answered_as_a_browser_out_of_reach(self) -> None:
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=_error_with_no_text()))
        )

        with (
            self.assertLogs("app.browser.services.runtime", "WARNING") as logged,
            self.assertRaises(BrowserUnreachableError) as raised,
        ):
            await runtime._connect_over_cdp(playwright, "ws://10.0.0.9:9222/devtools/browser/abc")

        message = (
            "could not connect to Chrome over CDP at 'ws://10.0.0.9:9222': '<its text could not be made: RuntimeError>'"
        )
        self.assertEqual(str(raised.exception), message)
        self.assertEqual(logged.output, [f"WARNING:app.browser.services.runtime:{message}"])


def _makes_a_text(argument: ast.expr) -> bool:
    """Whether ``argument`` makes a text before the call it is passed to runs, anywhere in it: str(x) or repr(x), an
    f-string, x.message, or a text made of one (cut, its first line)."""
    return any(
        _makes_text(node) or (isinstance(node, ast.Attribute) and node.attr == "message") for node in ast.walk(argument)
    )


def _makes_text(node: ast.AST) -> bool:
    """Whether ``node`` itself makes a text: an f-string, str(x), repr(x) or a .format()."""
    if isinstance(node, ast.JoinedStr):
        return True
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in {"str", "repr"}
    return isinstance(node.func, ast.Attribute) and node.func.attr == "format"


class NoTextMadeOutsideTheGuardTests(unittest.TestCase):
    def test_quoted_for_log_is_never_passed_a_text_made_for_it(self) -> None:
        # Such a text is an error's (str(exc), exc.message, str(task.exception())): error_text_for_log makes it.
        made = [f"{relative}:{line}" for relative, tree in _app_modules() for line in texts_made_for_quoting(tree)]
        self.assertEqual(made, [], "pass the error to error_text_for_log, which makes its text in its own try")


def _app_modules() -> list[tuple[str, ast.AST]]:
    """Each module of controller/app, by its path under app/ and its tree."""
    return [
        (path.relative_to(_APP).as_posix(), ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(_APP.rglob("*.py"))
    ]


def texts_made_for_quoting(tree: ast.AST) -> list[int]:
    """The lines in ``tree`` where quoted_for_log is passed a text made for it (see _makes_a_text)."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "quoted_for_log" and _makes_a_text(node.args[0]):
            lines.append(node.lineno)
    return lines


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

    async def test_the_line_of_a_cdp_connect_that_failed(self) -> None:
        # The first line of the error is kept, its URLs scrubbed: it was then written as it came, an ESC and all and
        # as long as Chrome made it, and so was the host of the URL (the caller's own, for browser.cdp_attach).
        first_line = "BrowserType.connect_over_cdp: forged by a page\x1b[2J" + "x" * 5000
        failed = PlaywrightError(first_line + "\nWARNING:app.main:forged\nCall log: ws://u:secret@10.0.0.9:9222/x")
        playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=failed)))

        with self.assertLogs("app.browser.services.runtime", "WARNING") as logged:
            with self.assertRaises(BrowserUnreachableError) as raised:
                await runtime._connect_over_cdp(playwright, "ws://u:secret@evil\x1bhost:9222/devtools/browser/abc")

        (written,) = _written(logged.records)
        self.assert_one_line(
            written,
            "could not connect to Chrome over CDP at 'ws://evil\\x1bhost:9222': "
            "'BrowserType.connect_over_cdp: forged by a page\\x1b[2J",
        )
        self.assertEqual(str(raised.exception), written)
        self.assertNotIn("secret", written)


_LOG_CALLS = frozenset({"debug", "info", "warning", "error", "critical", "exception", "log"})


def _a_text_made_for_the_line(argument: ast.expr) -> bool:
    """Whether ``argument`` makes a text before logging takes it, anywhere in it: an f-string, str(x), repr(x) or a
    .format(), cut or not."""
    return any(_makes_text(node) for node in ast.walk(argument))


def _a_message_made_for_the_line(message: ast.expr) -> bool:
    """Whether a log call's message is made in the call: an f-string, a call, or a text formatted with %. A message
    joined from constants and names (a route's own format) is not made from a caller's text."""
    return any(
        isinstance(node, ast.JoinedStr | ast.Call) or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod))
        for node in ast.walk(message)
    )


def _log_message(node: ast.Call) -> ast.expr | None:
    """A log call's message: its first argument, logger.log's second; None for a call passed none."""
    index = 1 if node.func.attr == "log" else 0
    return node.args[index] if len(node.args) > index else None


def log_texts_made_for_the_line(tree: ast.AST) -> list[int]:
    """The lines in ``tree`` of each log call whose message is made in the call, or that is passed a text made for
    it (see _a_message_made_for_the_line and _a_text_made_for_the_line)."""
    lines = []
    for node in ast.walk(tree):
        arguments = _log_arguments(node)
        if arguments is None:
            continue
        message = _log_message(node)
        if message is not None and _a_message_made_for_the_line(message):
            lines.append(message.lineno)
        lines += [argument.lineno for argument in arguments if _a_text_made_for_the_line(argument)]
    return lines


class NoLogArgumentMadeForTheLineTests(unittest.TestCase):
    def test_no_log_call_is_passed_a_text_made_or_cut_for_it(self) -> None:
        # Made before the call, an error's text is made outside any guard, and cut by slicing it is neither quoted
        # nor escaped: error_text_for_log does both (quoted_for_log for a text that is not an error's).
        made = [f"{relative}:{line}" for relative, tree in _app_modules() for line in log_texts_made_for_the_line(tree)]
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


# The functions that make an error's text in a guard of their own, by file.
_TEXT_GUARDS = {"utils.py": frozenset({"error_text", "error_text_for_log"})}
# Files that make an error's text without error_text, and why they cannot import it.
_NO_APP_UTILS = {
    "mcp_stdio.py": "the stdio bridge, byte-identical with the client package's copy, which has no app.utils",
    "drop_caps.py": "runs with the container's capabilities before it drops them, on ctypes, os and sys alone",
}


def _a_text_made_from_an_error(node: ast.AST, errors: set[str]) -> bool:
    """Whether ``node`` makes a text of one of ``errors``: str(e), repr(e), an f-string's {e} or str(e.args[i])."""
    if isinstance(node, ast.FormattedValue):
        return isinstance(node.value, ast.Name) and node.value.id in errors
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"str", "repr"}):
        return False
    argument = node.args[0] if node.args else None
    if isinstance(argument, ast.Subscript) and isinstance(argument.value, ast.Attribute):
        argument = argument.value.value if argument.value.attr == "args" else argument
    return isinstance(argument, ast.Name) and argument.id in errors


def texts_made_from_an_error(tree: ast.AST, guards: frozenset[str] = frozenset()) -> list[int]:
    """The lines in ``tree`` where a text is made from an error outside the functions named in ``guards``."""
    lines: list[int] = []

    def visit(node: ast.AST, errors: set[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in guards:
            return
        if _a_text_made_from_an_error(node, errors):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, errors | _error_names(child))

    visit(tree, set())
    return sorted(set(lines))


class NoTextMadeFromAnErrorOutsideTheGuardTests(unittest.TestCase):
    def test_every_text_taken_from_an_error_is_taken_through_error_text(self) -> None:
        # str() of an error raises where its text cannot be made, out of the except that took it: a log line, an
        # exception's message and an answer each lost the error they were about, and a check by its text (whether a
        # CDP session went with its target) raised in place of its answer.
        made: list[str] = []
        for path in sorted(_APP.rglob("*.py")):
            relative = path.relative_to(_APP).as_posix()
            if relative in _NO_APP_UTILS:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            guards = _TEXT_GUARDS.get(relative, frozenset())
            made += [f"{relative}:{line}" for line in texts_made_from_an_error(tree, guards)]
        self.assertEqual(made, [], "take the error's text with error_text, or error_text_for_log for a log line")


_CONVERSION = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?([diouxXeEfFgGcrsa%])")
# The %r arguments a log line keeps, by file and argument: none is a caller's, a peer's, a page's or a browser's text.
_KEPT_REPR = {
    ("cdp/passthrough.py", "method"): "one of _ALLOWED_CDP_COMMANDS, past the allowlist check",
    ("compliance.py", "normalized"): "an alias of _DEPRECATED_ALIASES",
    ("compliance.py", "target"): "the preset that alias names",
    ("compliance.py", "value"): "a preset's own value",
    ("compliance.py", "current"): "the operator's setting the preset replaces",
    ("curator_llm.py", "provider"): "the operator's CURATOR_PROVIDER",
    ("middleware/http.py", "operator_id"): "a configured credential's operator id",
    ("tool_gateway/selection.py", "value"): "the operator's MCP_TOOL_PROFILE",
}


def reprs_in_log_formats(tree: ast.AST) -> list[tuple[int, str]]:
    """Each %r in a log call's format in ``tree``: the line, and the source of the argument it writes."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        arguments = _log_arguments(node)
        if arguments is None:
            continue
        message = _log_message(node)
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
            continue
        conversions = [match.group(1) for match in _CONVERSION.finditer(message.value) if match.group(1) != "%"]
        for conversion, argument in zip(conversions, arguments, strict=False):
            if conversion == "r":
                found.append((node.lineno, ast.unparse(argument)))
    return found


class NoCallersTextWrittenByReprTests(unittest.TestCase):
    def test_a_log_format_writes_by_repr_only_what_no_caller_chose(self) -> None:
        # %r escapes a control character but writes the value whole: a peer's capability of 60,000 characters was a
        # record of 60,069. quoted_for_log quotes it the same way, and cuts it.
        written: list[str] = []
        for path in sorted(_APP.rglob("*.py")):
            relative = path.relative_to(_APP).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            written += [
                f"{relative}:{line} {argument}"
                for line, argument in reprs_in_log_formats(tree)
                if (relative, argument) not in _KEPT_REPR
            ]
        self.assertEqual(written, [], "write a caller's value with %s and quoted_for_log")


def _module(line: str) -> ast.AST:
    """A module whose except clause (its error named exc) holds ``line``, at _LINE; error is an error parameter."""
    return ast.parse(
        "def f(logger, task, value, during, error: BaseException):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        f"        {line}\n"
    )


_LINE = 5
# Mutation cases: each shape a scan exists to refuse, as a line of such a module, which the scan must name.
_REFUSED = [
    (
        "quoted_for_log",
        texts_made_for_quoting,
        [
            "quoted_for_log(str(exc))",
            "quoted_for_log(str(exc)[:200])",
            "quoted_for_log(str(exc).splitlines()[0])",
            "quoted_for_log(repr(exc))",
            "quoted_for_log(exc.message)",
            'quoted_for_log(f"{exc}")',
        ],
    ),
    (
        "made for the line",
        log_texts_made_for_the_line,
        [
            'logger.warning("failure: %s", f"{exc}")',
            'logger.warning("failure: %s", str(exc)[:200])',
            'logger.warning("failure: %s", repr(exc))',
            'logger.warning("failure: %s", "{}".format(exc))',
            'logger.warning(f"failure: {exc}")',
            'logger.warning("failure: %s" % exc)',
            'logger.warning("failure: {}".format(exc))',
            'logger.log(30, f"failure: {exc}")',
        ],
    ),
    (
        "passed whole",
        errors_passed_whole,
        [
            'logger.warning("failure: %s", exc)',
            'logger.warning("failure: %s", exc.message)',
            'logger.warning("failure: %s", exc.args)',
            'logger.warning("failure: %s", exc.args[0])',
            'logger.warning("failure: %s", task.exception())',
            'logger.warning("failure: %s", error)',
            'logger.log(30, "failure: %r", exc)',
        ],
    ),
    (
        "made from an error",
        texts_made_from_an_error,
        ['raise ValueError(f"failure: {exc}")', "text = str(exc)", "text = repr(exc)", "text = str(exc.args[0])"],
    ),
    (
        "by repr",
        lambda tree: [line for line, _argument in reprs_in_log_formats(tree)],
        ['logger.info("value=%r", value)', 'logger.log(20, "value=%r", value)'],
    ),
]
# The shapes that take the text through a guard: no scan names them.
_GUARDED = [
    'logger.warning("failure: %s", error_text_for_log(exc))',
    'logger.warning("failure: %s: %s", type(exc).__name__, error_text_for_log(error))',
    'logger.warning("failure during " + during + ": %s", error_text_for_log(exc))',
    'logger.info("value=%s", quoted_for_log(value))',
    "quoted_for_log(value[:10])",
    'raise ValueError(f"failure: {error_text(exc)}"[:300])',
]


class TheScansRefuseEachShapeTests(unittest.TestCase):
    def test_each_scan_names_each_shape_it_refuses(self) -> None:
        for name, scan, lines in _REFUSED:
            for line in lines:
                with self.subTest(scan=name, line=line):
                    self.assertEqual(set(scan(_module(line))), {_LINE})

    def test_no_scan_names_a_text_taken_through_a_guard(self) -> None:
        for line in _GUARDED:
            for name, scan, _lines in _REFUSED:
                with self.subTest(scan=name, line=line):
                    self.assertEqual(scan(_module(line)), [])


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


class AValidationErrorAnsweredTests(unittest.TestCase):
    def test_error_text_names_each_refused_field_and_its_kind_and_never_its_value(self) -> None:
        # An answer and an exception's message take error_text: it was str() of the error, which pydantic writes
        # over several lines with each value it refused (the first and last characters of a long one).
        class Stored(BaseModel):
            goal: str
            steps: dict[str, int]

        value = "a value that must not come back"
        with self.assertRaises(ValidationError) as refused:
            Stored.model_validate({"goal": [value], "steps": {f"step-{index}": value for index in range(40)}})

        text = error_text(refused.exception)

        fields = ["goal: string_type", *(f"steps.step-{index}: int_parsing" for index in range(40))]
        # Every field, unquoted and uncut: an answer cuts its own text where it did.
        self.assertEqual(text, "41 refused by Stored: " + "; ".join(fields))
        self.assertGreater(len(text), MAX_LOGGED_PATH_CHARS)

    def test_error_text_of_any_other_error_is_its_own_text(self) -> None:
        self.assertEqual(error_text(ValueError("Job is not resumable\nas it was")), "Job is not resumable\nas it was")
        self.assertEqual(error_text("a KeyError's first argument"), "a KeyError's first argument")


if __name__ == "__main__":
    unittest.main()
