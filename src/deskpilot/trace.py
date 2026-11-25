"""Append-only JSONL trace recorder with correlation IDs and redaction."""

import json
import threading
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.redaction import MAX_LINE_BYTES, redact

PREVIEW_CHARS = 500


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    MODEL_CALL = "model_call"
    MODEL_RESULT = "model_result"
    TOOL_CALL = "tool_call"
    POLICY_DECISION = "policy_decision"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    ERROR = "error"
    MODEL_ESCALATED = "model_escalated"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    CACHE_STORED = "cache_stored"
    CACHE_INVALIDATED = "cache_invalidated"
    RUN_FINISHED = "run_finished"


class Outcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class TraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    ts: datetime
    run_id: str
    event_id: str
    seq: int
    kind: EventKind
    operation_id: str | None = None
    parent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _new_id() -> str:
    return uuid.uuid4().hex


def _dump(event: TraceEvent) -> str:
    return json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _run_exists(path: Path, run_id: str) -> bool:
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                if json.loads(line).get("run_id") == run_id:
                    return True
            except (ValueError, AttributeError):
                continue
    return False


class TraceRecorder:
    def __init__(self, path: Path, run_id: str | None = None, secrets: Iterable[str] = ()) -> None:
        self._path = Path(path)
        self._run_id = run_id or _new_id()
        if _run_exists(self._path, self._run_id):
            raise ValueError(f"run_id already present in trace file: {self._run_id}")
        self._secrets = tuple(secrets)
        self._lock = threading.Lock()
        self._seq = 0
        self._ids: set[str] = set()
        self._started = False
        self._finished = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8", newline="\n")

    @property
    def run_id(self) -> str:
        return self._run_id

    def __enter__(self) -> "TraceRecorder":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def new_operation(self) -> str:
        return _new_id()

    def start_run(self, goal: str, **payload: Any) -> str:
        return self._emit(EventKind.RUN_STARTED, {"goal": goal, **payload})

    def finish(self, outcome: Outcome, stop_reason: str, **payload: Any) -> str:
        data = {"outcome": Outcome(outcome).value, "stop_reason": stop_reason, **payload}
        return self._emit(EventKind.RUN_FINISHED, data)

    def emit(
        self,
        kind: EventKind,
        payload: Mapping[str, Any] | None = None,
        *,
        operation_id: str | None = None,
        parent_id: str | None = None,
    ) -> str:
        kind = EventKind(kind)
        if kind in (EventKind.RUN_STARTED, EventKind.RUN_FINISHED):
            raise ValueError("use start_run() / finish() for run boundary events")
        return self._emit(kind, payload, operation_id=operation_id, parent_id=parent_id)

    def _emit(
        self,
        kind: EventKind,
        payload: Mapping[str, Any] | None = None,
        *,
        operation_id: str | None = None,
        parent_id: str | None = None,
    ) -> str:
        kind = EventKind(kind)
        clean = redact(dict(payload or {}), secrets=self._secrets)
        with self._lock:
            if self._finished or self._file.closed:
                raise RuntimeError("trace is finished or closed")
            if kind is EventKind.RUN_STARTED and self._started:
                raise ValueError("run already started")
            if parent_id is not None and parent_id not in self._ids:
                raise ValueError(f"unknown parent_id: {parent_id}")
            event = TraceEvent(
                ts=datetime.now(UTC),
                run_id=self._run_id,
                event_id=_new_id(),
                seq=self._seq + 1,
                kind=kind,
                operation_id=operation_id,
                parent_id=parent_id,
                payload=clean,
            )
            line = _dump(event)
            size = len(line.encode("utf-8"))
            if size > MAX_LINE_BYTES:
                preview = json.dumps(clean, ensure_ascii=False)[:PREVIEW_CHARS]
                summary = {"truncated": True, "original_bytes": size, "preview": preview}
                event = event.model_copy(update={"payload": summary})
                line = _dump(event)
            self._file.write(line + "\n")
            self._file.flush()
            self._seq = event.seq
            self._ids.add(event.event_id)
            self._started = self._started or kind is EventKind.RUN_STARTED
            self._finished = kind is EventKind.RUN_FINISHED
            return event.event_id

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


def read_trace(path: Path) -> list[TraceEvent]:
    with Path(path).open(encoding="utf-8") as handle:
        return [TraceEvent.model_validate_json(line) for line in handle if line.strip()]


def operation_chain(
    events: Iterable[TraceEvent], operation_id: str, run_id: str | None = None
) -> list[TraceEvent]:
    matches = (
        e
        for e in events
        if e.operation_id == operation_id and (run_id is None or e.run_id == run_id)
    )
    return sorted(matches, key=lambda e: e.seq)
