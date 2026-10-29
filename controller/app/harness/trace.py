from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
        if any(event_type.startswith("screenshot") for event_type in event_types):
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

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
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
            observation = step.get("observation") or {}
            if observation:
                final_observation = observation
            event = TraceEvent(
                run_id=trace.run_id,
                step_idx=index,
                event_type="action",
                payload=_redact_sensitive(
                    {
                        "status": step.get("status"),
                        "decision": decision,
                        "observation": observation,
                        "execution": step.get("execution"),
                    }
                ),
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
        trace.final_observation = _redact_sensitive(final_observation)
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
            final_observation=_redact_sensitive(final_observation or {}),
            network_entries=_redact_sensitive(network_entries or []),
            extracted_data=_redact_sensitive(extracted_data or {}),
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


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in _SENSITIVE_KEY_FRAGMENTS):
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return value
