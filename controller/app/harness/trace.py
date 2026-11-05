from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..network_inspector import scrub_url
from ..pii_scrub import PiiScrubber
from ..sqlite_utils import connect_sqlite

_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "set-cookie",
    "api-key",
    "api_key",
    "token",
    "password",
    "secret",
)
_REDACTED = "[REDACTED]"
# The keys that hold what an action typed. They are redacted when the action is marked sensitive.
_TYPED_TEXT_KEYS = frozenset({"text", "text_preview"})
_SENSITIVE_FLAG_KEYS = ("sensitive", "text_redacted")
# The keys of an agent step's observation that point at its screenshot.
_SCREENSHOT_KEYS = ("screenshot_path", "screenshot_url")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_URL_SCRUB_MAX_BYTES = 16384


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def hash_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    step_idx: int = 0
    event_type: str = Field(min_length=1, max_length=120)
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str = ""
    entry_hash: str = ""

    def with_hash(self) -> "TraceEvent":
        data = self.model_dump(exclude={"entry_hash"})
        self.entry_hash = hash_payload(data)
        return self


class TraceIntegrityError(ValueError):
    pass


class TraceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    contract_hash: str
    events: list[TraceEvent] = Field(default_factory=list)
    final_observation: dict[str, Any] = Field(default_factory=dict)
    network_entries: list[dict[str, Any]] = Field(default_factory=list)
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def evidence_kinds(self) -> set[str]:
        kinds = {"trace"}
        event_types = {event.event_type for event in self.events}
        if any(event_type.startswith("action") for event_type in event_types):
            kinds.add("actions")
        if any(_is_screenshot_evidence(event) for event in self.events):
            kinds.add("screenshots")
        if self.network_entries or any(event_type.startswith("network") for event_type in event_types):
            kinds.add("network")
        if any(event_type.startswith("console") for event_type in event_types):
            kinds.add("console")
        if any(event_type.startswith("model") or event_type == "decision" for event_type in event_types):
            kinds.add("model_decisions")
        return kinds

    @property
    def evidence(self) -> list[str]:
        return sorted(self.evidence_kinds())

    @property
    def trace_hash(self) -> str:
        return hash_payload(self.model_dump(mode="json", exclude={"metadata"}))

    def verify_chain(self) -> bool:
        previous_hash = ""
        for index, event in enumerate(self.events):
            if event.previous_hash != previous_hash:
                raise TraceIntegrityError(f"trace event {index} previous_hash mismatch")
            expected_hash = hash_payload(event.model_dump(exclude={"entry_hash"}))
            if event.entry_hash != expected_hash:
                raise TraceIntegrityError(f"trace event {index} entry_hash mismatch")
            previous_hash = event.entry_hash
        return True

    def scrubbed(self) -> "TraceEnvelope":
        """A copy that is safe to store: PII and URL tokens scrubbed from every string.

        The envelope the recorder returns keeps its observation, network entries and extracted data as the
        page gave them (secret-named keys aside), because the verifier checks those: scrubbing before the
        verdict turned a forbidden email into "[REDACTED]" and let a run pass. Whatever is written to disk
        or handed to the skill inducer goes through here first. Events are scrubbed when recorded.
        """
        return self.model_copy(
            update={
                "final_observation": _redact_sensitive(self.final_observation),
                "network_entries": _redact_sensitive(self.network_entries),
                "extracted_data": _redact_sensitive(self.extracted_data, strings=_scrub_urls_only),
            },
            deep=True,
        )

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(self.scrubbed().model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def read_json(cls, path: str | Path) -> "TraceEnvelope":
        trace = cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
        trace.verify_chain()
        return trace

    @classmethod
    def from_agent_result(
        cls,
        *,
        contract_hash: str,
        result: dict[str, Any],
        run_id: str | None = None,
    ) -> "TraceEnvelope":
        trace = cls(run_id=run_id or uuid.uuid4().hex[:12], contract_hash=contract_hash)
        previous_hash = ""
        final_observation: dict[str, Any] = {}
        for index, step in enumerate(result.get("steps") or [], start=1):
            if isinstance(step, BaseModel):
                step = step.model_dump()
            decision = step.get("decision") or {}
            if _execution_redacted_the_text(step.get("execution")):
                # The controller found the field sensitive at run time, whatever the agent marked.
                decision = {**decision, "sensitive": True}
            observation = step.get("observation") or {}
            if observation:
                final_observation = observation
            payloads: list[tuple[str, dict[str, Any]]] = []
            # The orchestrator observes, then decides and acts, so the observation's screenshot is the page
            # before this step's action: it comes first, then the decision (the type the mock attempt records,
            # which meets a "model_decisions" requirement), then the action.
            screenshot = {key: observation[key] for key in _SCREENSHOT_KEYS if observation.get(key)}
            if screenshot:
                payloads.append(("screenshot", screenshot))
            if decision:
                payloads.append(("model_decision", {"decision": decision}))
            payloads.append(
                (
                    "action",
                    {
                        "status": step.get("status"),
                        "decision": decision,
                        "observation": observation,
                        "execution": step.get("execution"),
                    },
                )
            )
            for event_type, payload in payloads:
                event = TraceEvent(
                    run_id=trace.run_id,
                    step_idx=index,
                    event_type=event_type,
                    payload=_redact_sensitive(payload),
                    previous_hash=previous_hash,
                ).with_hash()
                previous_hash = event.entry_hash
                trace.events.append(event)
        final_session = result.get("final_session") or {}
        if final_session:
            final_observation = {
                **final_observation,
                "url": final_session.get("current_url") or final_observation.get("url"),
                "title": final_session.get("title") or final_observation.get("title"),
            }
        trace.final_observation = _redact_sensitive(final_observation, strings=None)
        trace.metadata["source"] = "agent_result"
        return trace


class TraceRecorder:
    def __init__(self, root: str | Path, *, run_id: str, contract_hash: str, nest_run_dir: bool = True):
        self.root = Path(root)
        self.run_id = run_id
        self.contract_hash = contract_hash
        self.run_dir = self.root / "runs" / run_id if nest_run_dir else self.root
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.run_dir / "trace.jsonl"
        self.db_path = self.root / "trace-index.sqlite3"
        self._previous_hash = ""
        self._startup_db()

    def append(self, event_type: str, payload: dict[str, Any], *, step_idx: int = 0) -> TraceEvent:
        event = TraceEvent(
            run_id=self.run_id,
            step_idx=step_idx,
            event_type=event_type,
            payload=_redact_sensitive(payload),
            previous_hash=self._previous_hash,
        ).with_hash()
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        self._insert_event(event)
        self._previous_hash = event.entry_hash
        return event

    def finalize(
        self,
        *,
        final_observation: dict[str, Any] | None = None,
        network_entries: list[dict[str, Any]] | None = None,
        extracted_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEnvelope:
        events = []
        if self.jsonl_path.exists():
            # On "\n" only: splitlines() also breaks at U+2028, U+2029 and U+0085, which an event's JSON holds raw.
            for line in self.jsonl_path.read_text(encoding="utf-8").split("\n"):
                if line.strip():
                    events.append(TraceEvent.model_validate_json(line))
        envelope = TraceEnvelope(
            run_id=self.run_id,
            contract_hash=self.contract_hash,
            events=events,
            final_observation=_redact_sensitive(final_observation or {}, strings=None),
            network_entries=_redact_sensitive(network_entries or [], strings=None),
            # The task's own output: its text is what the verifier checks, so only secret-named keys go.
            extracted_data=_redact_sensitive(extracted_data or {}, strings=None),
            metadata=metadata or {},
        )
        envelope.write_json(self.run_dir / "trace.json")
        return envelope

    def _startup_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_events (
                    run_id TEXT NOT NULL,
                    step_idx INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    entry_hash TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_run ON trace_events(run_id, step_idx)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_type ON trace_events(event_type)")
            conn.commit()
        finally:
            conn.close()

    def _insert_event(self, event: TraceEvent) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO trace_events (
                    run_id, step_idx, event_type, timestamp, entry_hash, previous_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.step_idx,
                    event.event_type,
                    event.timestamp,
                    event.entry_hash,
                    event.previous_hash,
                    canonical_json(event.payload),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path, timeout=10)


_scrubber_for: tuple[object, PiiScrubber] | None = None


def _pii_scrubber() -> PiiScrubber:
    """The scrubber for the current settings, rebuilt whenever `get_settings()` returns a different object."""
    global _scrubber_for
    settings = get_settings()
    if _scrubber_for is None or _scrubber_for[0] is not settings:
        _scrubber_for = (settings, PiiScrubber.from_settings(settings))
    return _scrubber_for[1]


def _is_screenshot_evidence(event: TraceEvent) -> bool:
    """A screenshot event that points at an image. The mock attempt's {"available": False} one does not."""
    if not event.event_type.startswith("screenshot") or event.payload.get("available") is False:
        return False
    return any(event.payload.get(key) for key in _SCREENSHOT_KEYS)


def _execution_redacted_the_text(execution: Any) -> bool:
    target = execution.get("target") if isinstance(execution, dict) else None
    return isinstance(target, dict) and bool(target.get("text_redacted"))


def _scrub_string(value: str) -> str:
    """URLs through the URL scrubber wherever they sit in the string, the rest through the text scrubber."""
    return _map_urls(value, _scrub_url, _scrub_text)


def _scrub_text(text: str) -> str:
    return _pii_scrubber().text(text).text


def _scrub_urls_only(value: str) -> str:
    """The URL scrubber on URL-shaped parts of a string; every other character is kept."""
    return _map_urls(value, _scrub_url, str)


def _scrub_url(url: str) -> str:
    return scrub_url(url, _pii_scrubber(), _URL_SCRUB_MAX_BYTES)[0]


def _map_urls(value: str, on_url: Callable[[str], str], on_text: Callable[[str], str]) -> str:
    parts: list[str] = []
    position = 0
    for match in _URL_PATTERN.finditer(value):
        parts.append(on_text(value[position : match.start()]))
        parts.append(on_url(match.group()))
        position = match.end()
    parts.append(on_text(value[position:]))
    return "".join(parts)


def _redact_sensitive(value: Any, *, strings: Callable[[str], str] | None = _scrub_string) -> Any:
    """Redact secret-named keys, typed text of sensitive actions, and (with `strings`) rewrite every string."""
    if isinstance(value, dict):
        hide_typed_text = any(value.get(flag) for flag in _SENSITIVE_FLAG_KEYS)
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in _SENSITIVE_KEY_FRAGMENTS):
                redacted[key] = _REDACTED
            elif hide_typed_text and key_text in _TYPED_TEXT_KEYS:
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_sensitive(item, strings=strings)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item, strings=strings) for item in value]
    if strings is not None and isinstance(value, str):
        return strings(value)
    return value
