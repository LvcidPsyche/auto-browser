from __future__ import annotations

import asyncio
import decimal
import enum
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
import traceback
import uuid
from collections import OrderedDict
from collections.abc import Coroutine
from datetime import date, datetime, timezone
from datetime import time as time_of_day
from pathlib import Path, PurePath
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

logger = logging.getLogger(__name__)

UTC = timezone.utc

# Strong references to fire-and-forget background tasks. The event loop only keeps
# a weak reference to a task, so a task scheduled without a saved reference can be
# garbage-collected before it finishes ("Task was destroyed but it is pending").
# Tasks remove themselves from this set on completion.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# mkstemp creates 0600 files; atomic_write_text gives the file the mode it should
# have (see _target_mode) before it replaces the target.
_UMASK = os.umask(0)
os.umask(_UMASK)

# What stores that hold credentials, tokens or typed text ask atomic_write_text for.
PRIVATE_FILE_MODE = 0o600

# A key whose lower-case name holds one of these names a secret, at any depth: the run trace redacts by them, and so
# does a restricted witness receipt.
SECRET_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "set-cookie",
    "api-key",
    "api_key",
    "token",
    "password",
    "secret",
)

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# The most characters of a caller's own text one log line carries: a request's path or Host, or an error that quotes
# what the caller sent. A server lets a request line run to megabytes, and each of these lines is written for a
# request that was refused.
MAX_LOGGED_PATH_CHARS = 200


def capped_for_log(text: str) -> str:
    """``text`` cut to MAX_LOGGED_PATH_CHARS for a log line, with "..." where something was cut."""
    return text if len(text) <= MAX_LOGGED_PATH_CHARS else text[:MAX_LOGGED_PATH_CHARS] + "..."


def quoted_for_log(value: object) -> str:
    """``value`` as a log line carries it: quoted, a line break in it escaped, and cut to MAX_LOGGED_PATH_CHARS with
    "..." after the closing quote where something was cut ('xyyy'...): inside the quotes, the mark read as the
    text's own last characters.

    A str is quoted as it is. Anything else prints itself as it likes, and a str subclass answers repr() as it
    likes, so what repr() gave is quoted in turn, by str's own repr.
    """
    try:
        text = value if type(value) is str else repr(value)
        quoted = str.__repr__(text[:MAX_LOGGED_PATH_CHARS])
        return quoted if len(text) <= MAX_LOGGED_PATH_CHARS else quoted + "..."
    except Exception:
        return f"<a {type(value).__name__} that could not be printed>"


# Of an error a record names by its frames (a route's 500, a worker's crash), the outermost (where it was caught, the
# caller's own line, the call it made: which path led there) and those nearest the raise (where, and the calls just
# before it), with the count of those between: a record a few lines long however deep the error was raised. Ten such
# frames reach from a service call into the library that raised; a frame says no more the hundredth time it repeats.
LOGGED_OUTERMOST_FRAMES = 3
LOGGED_NEAREST_FRAMES = 10
# The errors one was raised from or while handling, each named on a line, nearest first, as a traceback names them;
# past these, how many more there were.
LOGGED_CHAINED_ERRORS = 3
# The errors a group (an ExceptionGroup, a TaskGroup's) holds, named the same way, at any depth; past these, how many
# more it held.
LOGGED_GROUP_MEMBERS = 3


def error_text(error: object) -> str:
    """What str() makes of ``error`` (an error, or what one carries, such as its first argument), made so that it
    never raises: a text that cannot be made (str(KeyError(x)) is repr(x), and an object's repr can raise) is named
    as error_text_for_log names it, and the except that takes it goes on. Of a pydantic ValidationError, as there,
    the fields it refused and never the values a model was given (_refused_fields).

    For an answer, an exception's message or a test of the text, which cut it as they did: neither quoted nor cut.
    A log line takes error_text_for_log.
    """
    try:
        return _refused_fields(error) if isinstance(error, ValidationError) else str(error)
    except Exception as failed:
        return _text_not_made(failed)


def _text_not_made(failed: BaseException) -> str:
    return f"<its text could not be made: {type(failed).__name__}>"


def _refused_fields(error: ValidationError) -> str:
    """A pydantic ValidationError's text without what pydantic's own prints, the value of each field it refused (a
    body holds a webhook key or a password): which field and what kind of refusal, no more."""
    refused = error.errors(include_url=False, include_context=False, include_input=False)
    fields = "; ".join(f"{'.'.join(str(part) for part in entry['loc'])}: {entry['type']}" for entry in refused)
    return f"{error.error_count()} refused by {error.title}: {fields}"


def error_text_for_log(error: BaseException) -> str:
    """The error's own text for the log: on one line, cut, and never the values a model was given."""
    try:
        text = _refused_fields(error) if isinstance(error, ValidationError) else str(error)
        cut = "" if len(text) <= MAX_LOGGED_PATH_CHARS else f" (cut from {len(text)} characters)"
        return quoted_for_log(text) + cut
    except Exception as failed:
        return _text_not_made(failed)


def error_lines_for_log(
    error: BaseException, *, outermost: int = LOGGED_OUTERMOST_FRAMES, nearest: int = LOGGED_NEAREST_FRAMES
) -> str:
    """Where ``error`` was raised, and what it chained, for the lines under a record's message.

    In place of the traceback exc_info writes, which prints each error's text as it is (a page's, a caller's): a
    frame a line (the file, the line and the function), the ``outermost`` and the ``nearest`` with the count of
    those between, or with no outermost the nearest alone; then, of a group, the errors it holds; then each error it
    was raised from or while handling, and of such a group the errors it holds.
    A frame as the interpreter prints it is two lines on Python 3.12 and, from 3.13, as many as its call is written
    over, so a record would be as long as the Python it ran on made it.
    """
    frames = _frame_lines(error.__traceback__, outermost, nearest)
    return "\n".join([*frames, *_group_members(error, "  "), *_chained_errors(error)])


def _frame_line(frame: traceback.FrameSummary) -> str:
    return f"  {frame.filename}:{frame.lineno} in {frame.name}"


def _frame_lines(tb: TracebackType | None, outermost: int, nearest: int) -> list[str]:
    if outermost == 0:
        return [_frame_line(frame) for frame in traceback.extract_tb(tb, limit=-nearest)]
    total = sum(1 for _ in traceback.walk_tb(tb))
    if total <= outermost + nearest:
        return [_frame_line(frame) for frame in traceback.extract_tb(tb)]
    first = [_frame_line(frame) for frame in traceback.extract_tb(tb, limit=outermost)]
    last = [_frame_line(frame) for frame in traceback.extract_tb(tb, limit=-nearest)]
    return [*first, f"  ... {total - outermost - nearest} frames left out", *last]


def _chained_errors(error: BaseException) -> list[str]:
    """A line for each error ``error`` was raised from (__cause__) or while handling (__context__, unless the raise
    suppressed it), as a traceback names them: the nearest LOGGED_CHAINED_ERRORS by class and text, quoted and cut,
    and how many more there were. A record that left them out told a wrapped error only by its wrapper."""
    lines: list[str] = []
    seen, named, more = {id(error)}, 0, 0
    while True:
        if error.__cause__ is not None:
            how, error = "from", error.__cause__
        elif error.__context__ is not None and not error.__suppress_context__:
            how, error = "while handling", error.__context__
        else:
            break
        if id(error) in seen:  # a chain that comes back to an error it named, which a traceback stops at too
            break
        seen.add(id(error))
        if named < LOGGED_CHAINED_ERRORS:
            named += 1
            lines.append(f"  raised {how} {type(error).__name__}: {error_text_for_log(error)}")
            lines += _group_members(error, "    ")
        else:
            more += 1
    if more:
        lines.append(f"  and {more} more before them")
    return lines


def _group_members(error: BaseException, indent: str) -> list[str]:
    """A line for each error ``error`` holds, if it is a group (BaseExceptionGroup), in the order a traceback prints
    them, a group's own members under it a step further in: the first LOGGED_GROUP_MEMBERS by class and text, quoted
    and cut, and how many more it held at any depth. Walked with a list and not by recursion, and each error once (a
    group can hold one twice, so forty levels of such groups hold the first error 2**40 times): a group nested however
    deep is one pass over the errors it holds."""
    if not isinstance(error, BaseExceptionGroup):
        return []
    lines: list[str] = []
    seen, more = {id(error)}, 0
    pending = [(member, 0) for member in reversed(error.exceptions)]
    while pending:
        member, depth = pending.pop()
        if id(member) in seen:
            continue
        seen.add(id(member))
        if len(lines) < LOGGED_GROUP_MEMBERS:
            lines.append(f"{indent}{'  ' * depth}holding {type(member).__name__}: {error_text_for_log(member)}")
        else:
            more += 1
        if isinstance(member, BaseExceptionGroup):
            pending += [(inner, depth + 1) for inner in reversed(member.exceptions)]
    if more:
        lines.append(f"{indent}and {more} more")
    return lines


# A caller refused ahead of the rate limiter, or one that needs no credential to be refused, can be refused without
# end, and a warning a refusal fills the log: one line a source a window, and the next says how many were only counted.
REFUSAL_LOG_WINDOW_SECONDS = 60.0
REFUSAL_LOG_SOURCES_MAX = 1024


class RefusalLogWindows:
    """Which refusals of one kind are logged: a source's first, then one a window with the count held back.

    Bounded as the takeover-link refusals are (routes/sessions.py): the source logged longest ago is forgotten
    first, with its count, and logs as new.
    """

    def __init__(self) -> None:
        self._windows: OrderedDict[str, list[float]] = OrderedDict()  # source -> [window start, refusals counted]

    def due(self, source: str, *, now: float) -> int | None:
        """How many refusals the line due now reports as held back (0: none), or None: this one is only counted."""
        window = self._windows.get(source)
        if window is not None and now - window[0] < REFUSAL_LOG_WINDOW_SECONDS:
            window[1] += 1
            return None
        self._windows[source] = [now, 0]
        self._windows.move_to_end(source)
        while len(self._windows) > REFUSAL_LOG_SOURCES_MAX:
            self._windows.popitem(last=False)
        return int(window[1]) if window is not None else 0


def held_back_note(held_back: int, source: str) -> str:
    """What a line that is due adds for the refusals only counted since the one before it ("" when there are none)."""
    return f" ({held_back} more from this {source} were refused since the last such line)" if held_back else ""


# A UTF-16 surrogate code point, which a Python string holds only on its own (a JSON "\ud800" escape
# decodes to one, and a valid pair decodes to the character it encodes). UTF-8 cannot encode it.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def without_lone_surrogates(value: Any) -> Any:
    """``value`` with each lone surrogate in its strings, dict keys included, replaced by U+FFFD.

    For a record about to be written as UTF-8 (a JSONL line, a witness receipt): a string holding one
    could not be encoded, so the write failed after the action it records had run.

    Two keys of one dict that differ only in a lone surrogate take the same name once it is replaced, and one
    value would go with the other's key. Neither is dropped: a key that needed no change keeps its name, and a
    changed key whose name is taken (by such a key, or by a changed key before it) gets " (2)" appended, or
    " (3)" and so on, the first that is free. A dict whose keys do not meet comes back under the names it had.
    """
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    if isinstance(value, dict):
        # Each key and each item is cleaned here, once: cleaned again where the keys meet, a dict of such dicts
        # cost twice the work for every level it went down.
        entries = [(key, without_lone_surrogates(key), without_lone_surrogates(item)) for key, item in value.items()]
        cleaned = {name: item for _, name, item in entries}
        # Fewer keys than it had: the replacement gave two of them one name.
        return cleaned if len(cleaned) == len(value) else _with_no_key_lost(entries)
    if isinstance(value, list | tuple):
        return [without_lone_surrogates(item) for item in value]
    return value


def _with_no_key_lost(entries: list[tuple[Any, Any, Any]]) -> dict[Any, Any]:
    """What without_lone_surrogates returns for a dict whose keys meet once the replacement is made (see there).

    ``entries`` holds, for each of the dict's items in order, its key as it was, that key cleaned and the item
    cleaned: this only gives the names out, and cleans nothing.
    """
    taken = {name for key, name, _ in entries if name == key}
    # The last number given to a name. A name once taken stays taken, so no number below it is free again; counted
    # up from " (2)" for each key, keys that all met cost the square of their count.
    last_number: dict[Any, int] = {}
    cleaned: dict[Any, Any] = {}
    for key, wanted, item in entries:
        name = wanted
        if name != key:
            number = last_number.get(wanted, 1)
            while name in taken:
                number += 1
                name = f"{wanted} ({number})"
            last_number[wanted] = number
        taken.add(name)
        cleaned[name] = item
    return cleaned


def holds_lone_surrogate(value: Any) -> bool:
    """Whether a string anywhere in ``value``, a dict key included, holds a lone surrogate."""
    if isinstance(value, str):
        return _LONE_SURROGATE.search(value) is not None
    if isinstance(value, dict):
        return any(holds_lone_surrogate(key) or holds_lone_surrogate(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(holds_lone_surrogate(item) for item in value)
    return False


def holds_lone_surrogate_in_a_key(value: Any) -> bool:
    """Whether a dict key anywhere in ``value`` holds a lone surrogate (its other strings are not looked at).

    Walked with a list and not by recursion: it is run on a caller's value that nothing has bounded the depth of.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(isinstance(key, str) and _LONE_SURROGATE.search(key) is not None for key in item):
                return True
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return False


# Types strict JSON has no form for that a result may still carry, sent as their text.
_SENT_AS_TEXT = (date, time_of_day, PurePath, uuid.UUID, decimal.Decimal)


def json_safe(value: Any) -> Any:
    """``value`` with only what strict JSON can hold: NaN and the infinities become null, a tuple or set a list,
    an Enum its value, and a known type (_SENT_AS_TEXT) its text.

    Any other object raises TypeError rather than going out as its ``str()``: a repr is not a result anyone shaped,
    and can hold what model_dump would have left out (a BrowserSession's holds its totp_secret).
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {json_key(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if isinstance(value, _SENT_AS_TEXT):
        return str(value)
    raise TypeError(f"a {type(value).__name__} is not sent in a tool result")


def json_key(key: Any) -> str:
    """A dict key as json.dumps spells it (``1``, ``true``, ``null``), or a known type's text; any other refused."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return json.dumps(key)
    if isinstance(key, _SENT_AS_TEXT):
        return str(key)
    raise TypeError(f"a {type(key).__name__} key is not sent in a tool result")


def record_json(record: BaseModel, *, indent: int | None = None) -> str:
    """``record`` as the JSON a store writes, with each lone surrogate in its strings replaced by U+FFFD.

    ``model_dump_json`` raises on one, and a page's title or a model's reply can hold one: the store then lost the
    record of a change that had been made. Only a record that fails the normal dump is walked.
    """
    try:
        return record.model_dump_json(indent=indent)
    except PydanticSerializationError:
        clean = type(record).model_validate(without_lone_surrogates(record.model_dump()))
        return clean.model_dump_json(indent=indent)


def record_path(root: Path, record_id: str, suffix: str) -> Path:
    """The file a file-backed store keeps ``record_id`` in, for one plain name only.

    Record ids reach the stores from MCP tool arguments, which — unlike REST path
    segments — may contain "/" and "..". Joined into the store root unchecked, an
    id such as "../audit/events" read a file outside it. Raised as KeyError
    because, to every caller, an id that cannot name a record names no record.
    """
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        raise KeyError(record_id)
    return root / f"{record_id}{suffix}"


def _target_mode(path: Path, mode: int | None) -> int:
    """The permission bits the replacement of ``path`` gets.

    An explicit ``mode`` wins, so a store that holds secrets can ask for 0600 and
    tighten a file an older release left wider. Otherwise the file keeps the mode
    it has (a 0600 file must not become 0644 because it was saved again), and a
    file that does not exist yet gets what a plain open() would have made.
    """
    if mode is not None:
        return mode & 0o777
    try:
        return stat.S_IMODE(os.stat(path).st_mode) & 0o777
    except FileNotFoundError:
        return 0o666 & ~_UMASK


# Windows refuses a rename while another process has the target open (an
# antivirus scan, an indexer, a reader thread); that clears within milliseconds.
RENAME_ATTEMPTS = 5
RENAME_RETRY_STEP_SECONDS = 0.01


def replace_retrying(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """os.replace, retried briefly through a sharing violation (PermissionError), then raised."""
    for attempt in range(RENAME_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_RETRY_STEP_SECONDS * (attempt + 1))


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``text`` so readers only ever see a whole file.

    Each call writes to its own temp file in the target directory. The stores
    used to share one fixed ``<name>.json.tmp`` per record, so two concurrent
    saves of the same record (they run in worker threads) raced: one writer's
    rename moved the other's temp file away, failing it with FileNotFoundError,
    or both wrote into the same temp file and a torn mix of the two was renamed
    into place. The temp name keeps a ``.tmp`` suffix so ``*.json`` globs skip it.

    ``mode`` sets the file's permission bits (pass ``PRIVATE_FILE_MODE`` for
    anything that holds a credential, token or typed text). Without it the
    file keeps the mode it already has.
    """
    target_mode = _target_mode(path, mode)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, target_mode)
        replace_retrying(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def utc_now() -> str:
    """Return current UTC timestamp as ISO-8601 string with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def spawn_background_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule a fire-and-forget coroutine while retaining a strong reference.

    Use instead of a bare ``asyncio.ensure_future``/``create_task`` whenever the
    result is not awaited, so the task cannot be collected mid-execution.
    """
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(_log_background_task_exception)
    return task


async def run_to_completion(work: Coroutine[Any, Any, Any]) -> Any:
    """Await ``work`` as its own task, which a cancel aimed at the caller does not stop; the cancel is raised once the
    work has finished, and its result is returned otherwise."""
    task = asyncio.ensure_future(work)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


def _log_background_task_exception(task: asyncio.Task[Any]) -> None:
    """Surface failures in fire-and-forget tasks.

    Nothing awaits these, so without this an exception only ever appeared as
    asyncio's "exception was never retrieved" warning at garbage-collection
    time — long after the fact, unattributed, and easy to miss entirely.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # Not by exc_info, which prints the error's text as it is: a page's, in a Playwright error.
        logger.error(
            "background task %s failed: %s: %s\n%s",
            task.get_name(),
            type(exc).__name__,
            error_text_for_log(exc),
            error_lines_for_log(exc),
        )
