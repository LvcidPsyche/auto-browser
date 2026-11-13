"""A log line makes an error's text inside the guard that quotes and cuts it, so a text that cannot be made is named.

A line written as quoted_for_log(str(exc)) made the text before the helper's own guard: an error whose text cannot be
made (str(KeyError(x)) is repr(x), and an object's repr can raise) raised out of the except it was logged in. As a
%s argument, logging made it lazily and reported a failed format on stderr; error_text_for_log makes it in its own try.

The scans below read the source, and take for an error what goes by a name they know (_error_names: an except
clause's own, a parameter named or annotated as one) in a shape they know (_an_error). What they cannot see: an error
under any other name (one assigned to a variable or an attribute, kept in a list, returned by a helper, read with
sys.exception()); a text already taken from one, or another program's own (a log file's tail), and then logged under
a name of its own; and a log format that is not written in the call.

Nor can they tell a caller's value from the controller's own. So each value written by its repr is listed, in a log
format (reprs_in_log_formats) or anywhere else (values_written_by_repr), and passes only where a kept list names its
file and its source text with the reason it is no caller's: a new one is a decision, not a line a scan lets by. A
kept entry that came to name another value in the same file, by the same source text, is not seen.
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

from app import api_token
from app.action_errors import BrowserUnreachableError
from app.browser import prefetch
from app.browser.request_gate import RequestGate
from app.browser.services import runtime
from app.browser.services.sessions import BrowserSessionService
from app.maintenance import CleanupStats, MaintenanceService
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

    def test_the_line_of_a_cleanup_entry_that_could_not_be_handled(self) -> None:
        # Passed to logging as a fallback (the error's strerror, or the error where it has none), either as it came.
        path = Path("uploads") / "a.bin"
        for failed in (OSError(13, FORGED), RuntimeError(FORGED)):
            with self.subTest(failed=type(failed).__name__):
                stats = CleanupStats(root="uploads", retention_hours=1)
                with self.assertLogs("app.maintenance", "WARNING") as logged:
                    MaintenanceService._entry_error(stats, path, failed, set())

                (written,) = _written(logged.records)
                self.assertEqual(stats.errors, 1)
                self.assert_one_line(written, f"cleanup sweep could not handle {path}: '")

    def test_the_line_of_a_token_file_whose_mode_could_not_be_checked(self) -> None:
        def stat(_path: Path) -> None:
            raise OSError(5, FORGED)

        # An os of the POSIX branch, which a Windows host does not take.
        with (
            patch.object(api_token, "os", SimpleNamespace(name="posix", stat=stat)),
            self.assertLogs("app.api_token", "WARNING") as logged,
        ):
            kept = api_token._check_the_mode_was_kept(Path("api-token"), "API_TOKEN_FILE")

        (written,) = _written(logged.records)
        self.assertEqual(kept, "its mode could not be checked")
        self.assert_one_line(
            written, "API_TOKEN_FILE: could not check the mode of api-token after writing it ('[Errno 5] p\\nWARNING:"
        )


_LOG_CALLS = frozenset({"debug", "info", "warning", "error", "critical", "exception", "log"})


def _a_text_made_for_the_line(argument: ast.expr) -> bool:
    """Whether ``argument`` makes a text before logging takes it, anywhere in it: an f-string, str(x), repr(x) or a
    .format(), cut or not; or holds an error's text as error_text() gives it, neither quoted nor cut."""
    return any(_makes_text(node) for node in ast.walk(argument)) or _an_unquoted_error_text(argument)


def _an_unquoted_error_text(node: ast.AST) -> bool:
    """Whether ``node`` holds an error_text() call that no quoted_for_log() call encloses."""
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in {"quoted_for_log", "error_text"}:
            return name == "error_text"
    return any(_an_unquoted_error_text(child) for child in ast.iter_child_nodes(node))


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


def _an_error(node: ast.expr, errors: set[str]) -> bool:
    """Whether ``node`` is an error or what one holds: one of ``errors``, its .args, an item of either (exc.args[0],
    or exc[1] of an exc_info; not err["msg"], which is a dict's), a task's .exception(), or a fallback (``a or b``,
    ``a if c else b``) either side of which is."""
    if isinstance(node, ast.BoolOp):
        return any(_an_error(value, errors) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _an_error(node.body, errors) or _an_error(node.orelse, errors)
    if isinstance(node, ast.Subscript) and not _a_text(node.slice):
        node = node.value
    if isinstance(node, ast.Attribute) and node.attr == "args":
        node = node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == "exception" and not node.args
    return isinstance(node, ast.Name) and node.id in errors


def _a_text(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _an_error_passed_whole(argument: ast.expr, errors: set[str]) -> bool:
    """Whether a log argument is an error itself (see _an_error), its .message or any item of one, or a fallback
    either side of which is (``exc.strerror or exc``): logging prints each as it is."""
    if isinstance(argument, ast.BoolOp):
        return any(_an_error_passed_whole(value, errors) for value in argument.values)
    if isinstance(argument, ast.IfExp):
        return _an_error_passed_whole(argument.body, errors) or _an_error_passed_whole(argument.orelse, errors)
    if isinstance(argument, ast.Subscript):
        argument = argument.value
    if isinstance(argument, ast.Attribute) and argument.attr == "message":
        argument = argument.value
    return _an_error(argument, errors)


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
    """Whether ``node`` makes a text of an error (see _an_error): str(e), repr(e) or format(e), an f-string's {e}, or
    a text formatted with it by % or by .format()."""
    if isinstance(node, ast.FormattedValue):
        return _an_error(node.value, errors)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        return any(_an_error(value, errors) for value in values)
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return any(_an_error(value, errors) for value in [*node.args, *(keyword.value for keyword in node.keywords)])
    if isinstance(node.func, ast.Name) and node.func.id in {"str", "repr", "format"}:
        return bool(node.args) and _an_error(node.args[0], errors)
    return False


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


# What writes a value by its repr outside a log call's format and is kept, by file and value (the source of what
# is written): none is a caller's, a peer's or a page's value as long as it was sent.
_KEPT_REPR_ELSEWHERE = {
    ("approval_display.py", "abs(value)"): "a number's own digits, read back as a Decimal",
    ("browser/element_ids.py", "str(element_id)[:80]"): "cut at 80 characters",
    ("browser/services/actions.py", "decision.action"): "one of the action names (ActionName)",
    ("browser_sandbox.py", "str(raw.get('sandbox'))[:32]"): "cut at 32 characters",
    ("cdp/passthrough.py", "name"): "a parameter the allowlisted command takes",
    ("cdp/passthrough.py", "shown"): "cut by cap_text",
    ("codex_sandbox.py", "mode"): "the operator's CODEX_SANDBOX_MODE",
    ("compliance.py", "raw"): "the operator's COMPLIANCE_TEMPLATE",
    ("curator_llm.py", "provider"): "the operator's CURATOR_PROVIDER",
    ("main.py", "_compliance_template"): "the operator's COMPLIANCE_TEMPLATE",
    ("mcp_stdio.py", "value"): "a form of a secret to take out of a text, never written",
    ("mesh/identity.py", "stored.get('node_id')"): "the node's own identity file",
    ("mesh/identity.py", "self._node_id"): "the node's own id",
    ("mesh/policy.py", "grant.capability"): "a grant of the operator's peers file",
    ("mesh/policy.py", "peer.node_id"): "a peer of the operator's peers file",
    ("mesh/policy.py", "peer_node_id"): "a peer of the operator's peers file",
    ("mesh/transport.py", "expected_peer.node_id"): "a peer of the operator's peers file",
    ("mesh/transport.py", "expected_recipient_node_id"): "the node's own id",
    ("mesh/transport.py", "peer.node_id"): "a peer of the operator's peers file",
    ("middleware/hosts.py", "pattern"): "the operator's CONTROLLER_ALLOWED_HOSTS",
    ("models.py", "host"): "a totp_hosts entry, at most 260 characters (CreateSessionRequest)",
    ("regex_guard.py", "text"): "a group of browser.find_elements' query, at most 500 characters (FindElementsInput)",
    ("runtime_policy.py", "pattern"): "the operator's CONTROLLER_ALLOWED_HOSTS",
    ("session_isolation.py", "self._controller_container_id"): "the controller's own container id",
    ("session_tunnel.py", "configured"): "the operator's ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS",
    ("sqlite_utils.py", "synchronous"): "a mode a store of the controller asks for",
    ("tool_gateway/drag.py", "end[:_SELECTOR_LIMIT]"): "cut at _SELECTOR_LIMIT",
    ("tool_gateway/gateway.py", "payload.storage_type"): "one of the input model's two literals",
    ("tool_gateway/registry.py", "spec.pack"): "a tool's own spec",
    ("tool_gateway/selection.py", "item"): "the operator's MCP_TOOL_PACKS",
    ("tool_gateway/selection.py", "packs"): "the operator's MCP_TOOL_PACKS",
    ("tool_gateway/selection.py", "profile"): "the operator's MCP_TOOL_PROFILE",
    ("utils.py", "value"): "quoted_for_log itself, which quotes what repr() gave and cuts it",
}
# Files whose every such value is kept, and why.
_KEPT_REPR_FILES = {
    "config.py": "the operator's settings, refused at the start",
    "harness/induce.py": "writes a test's source: each value is a Python literal there, whole",
    "playwright_export.py": "writes a script's source: each value is a Python literal there, whole",
}


# The conversions that write a value by its repr: %r, and %a, which is ascii() of it.
_BY_REPR = frozenset("ra")


def _conversions(text: str) -> list[str]:
    """The conversions of a %-format, in order: "s" and "r" for "%s is %r"."""
    return [match.group(1) for match in _CONVERSION.finditer(text) if match.group(1) != "%"]


def reprs_in_log_formats(tree: ast.AST) -> list[tuple[int, str]]:
    """Each %r in a log call's format in ``tree``: the line, and the source of the argument it writes. A format
    whose conversions and arguments differ in number is named in its place (which argument a %r writes cannot be
    told), and so is a %r in a format that is not one constant (one joined with +)."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        arguments = _log_arguments(node)
        message = _log_message(node) if arguments is not None else None
        if message is None:
            continue
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
            parts = [part.value for part in ast.walk(message) if _a_text(part)]
            if any(_BY_REPR & set(_conversions(part)) for part in parts):
                found.append((node.lineno, f"a %r of the format {ast.unparse(message)}"))
            continue
        conversions = _conversions(message.value)
        if len(conversions) != len(arguments):
            found.append((node.lineno, f"{len(arguments)} arguments for {len(conversions)} conversions"))
            continue
        for conversion, argument in zip(conversions, arguments, strict=True):
            if conversion in _BY_REPR:
                found.append((node.lineno, ast.unparse(argument)))
    return found


# A replacement field of str.format that converts by repr: {!r}, {0!r}, {name!a:>10}.
_FORMAT_REPR = re.compile(r"\{[\w.\[\]]*![ra](?::[^{}]*)?\}")


def values_written_by_repr(tree: ast.AST) -> list[tuple[int, str]]:
    """Each value ``tree`` writes by its repr outside a log call's format: the line, and the source of the value. An
    f-string's {x!r} or {x!a}, a repr(x) or ascii(x), and a %r or {!r} in a text that is no log call's format, which
    a % or a .format() fills (named by that text, since the value is not in it; not a %a there, which a script's
    ``% a`` and a docstring's %62a read as)."""
    formats = {id(_log_message(node)) for node in ast.walk(tree) if _log_arguments(node) is not None}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue) and node.conversion in (ord("r"), ord("a")):
            found.append((node.lineno, ast.unparse(node.value)))
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"repr", "ascii"} and node.args:
            found.append((node.lineno, ast.unparse(node.args[0])))
        elif _a_text(node) and id(node) not in formats:
            if "r" in _conversions(node.value) or _FORMAT_REPR.search(node.value):
                found.append((node.lineno, f"a format: {node.value}"))
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

    def test_an_answer_or_an_error_writes_by_repr_only_what_no_caller_chose(self) -> None:
        # An f-string's {x!r} in an answer or in an error's message does the same: a mesh peer's capability of 60,000
        # characters came back whole in the reply that refused its kind.
        written: list[str] = []
        kept: set[tuple[str, str]] = set()
        for relative, tree in _app_modules():
            if relative in _KEPT_REPR_FILES:
                continue
            for line, value in values_written_by_repr(tree):
                if (relative, value) in _KEPT_REPR_ELSEWHERE:
                    kept.add((relative, value))
                else:
                    written.append(f"{relative}:{line} {value}")
        self.assertEqual(written, [], "write a caller's value with quoted_for_log, or say why it is kept")
        self.assertEqual(sorted(set(_KEPT_REPR_ELSEWHERE) - kept), [], "kept, and no longer written")


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
            'logger.warning("failure: %s", error_text(exc))',
            'logger.warning("failure: %s", capped_for_log(error_text(exc)))',
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
            'logger.warning("failure: %s", exc.strerror or exc)',
            'logger.warning("failure: %s", getattr(exc, "strerror", None) or exc)',
            'logger.warning("failure: %s", exc.message or "no text")',
            'logger.warning("failure: %s", exc if value else "none")',
        ],
    ),
    (
        "made from an error",
        texts_made_from_an_error,
        [
            'raise ValueError(f"failure: {exc}")',
            "text = str(exc)",
            "text = repr(exc)",
            "text = str(exc.args[0])",
            "text = str(exc.args)",
            "text = format(exc)",
            'raise ValueError(f"failure: {exc.strerror or exc}")',
            'raise ValueError("failure: %s" % exc)',
            'raise ValueError("failure of %s: %s" % (value, exc))',
            'raise ValueError("failure: {}".format(exc))',
            'raise ValueError("failure: {reason}".format(reason=exc))',
            "text = str(task.exception())",
            'value.append(f"{during}: {exc[1]}")',
        ],
    ),
    (
        "by repr",
        lambda tree: [line for line, _argument in reprs_in_log_formats(tree)],
        [
            'logger.info("value=%r", value)',
            'logger.log(20, "value=%r", value)',
            'logger.info("value=%a", value)',
            'logger.info("during %s, value=%r", value)',
            'logger.info("during " + during + ", value=%r", value)',
        ],
    ),
    (
        "by repr elsewhere",
        lambda tree: [line for line, _value in values_written_by_repr(tree)],
        [
            'raise ValueError(f"Unknown capability kind: {value!r}")',
            'return {"error": f"Unknown capability kind: {value!r}"}',
            'raise ValueError(f"Unknown capability kind: {value!a}")',
            'raise ValueError("Unknown capability kind: " + repr(value))',
            'raise ValueError("Unknown capability kind: " + ascii(value))',
            'raise ValueError("Unknown capability kind: %r" % value)',
            'raise ValueError("Unknown capability kind: {!r}".format(value))',
        ],
    ),
]
# The shapes that take the text through a guard: no scan names them.
_GUARDED = [
    'logger.warning("failure: %s", error_text_for_log(exc))',
    'logger.warning("failure: %s: %s", type(exc).__name__, error_text_for_log(error))',
    'logger.warning("failure during " + during + ": %s", error_text_for_log(exc))',
    'logger.info("value=%s", quoted_for_log(value))',
    'logger.warning("failure: %s", quoted_for_log(value.sub("", error_text(exc))))',
    "quoted_for_log(value[:10])",
    'raise ValueError(f"failure: {error_text(exc)}"[:300])',
    'raise ValueError(f"failure: {exc.strerror or error_text(exc)}")',
    'raise ValueError(f"Unknown capability kind: {quoted_for_log(value)}")',
    'raise ValueError("%d of them: %s" % (len(value), error_text(exc)))',
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
