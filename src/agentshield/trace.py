"""Trusted, run-scoped observations exported separately from model context."""

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Any, Literal

from langchain_core.tools import ToolException
from pydantic import ValidationError

from agentshield.fixtures import FixtureRecord, PreparedScenario

EventKind = Literal[
    "run_started", "run_input", "state_snapshot", "model_started", "model_response", "model_failed",
    "tool_attempted", "arguments_validated", "policy_decision", "tool_result", "tool_failed",
    "dispatch_rejected", "run_finished",
]


def sanitize(value: Any, depth: int = 0) -> Any:
    """Bound untrusted fields and remove recognizable credentials before recording."""
    if depth > 6:
        return "[depth limit]"
    if isinstance(value, str):
        return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[redacted]", value)[:4000]
    if isinstance(value, dict):
        return {
            sanitize(str(key))[:128]: "[redacted]" if str(key).lower() in {
                "api_key", "openai_api_key", "authorization", "encrypted_content", "reasoning"
            } else sanitize(item, depth + 1)
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth + 1) for item in value[:40]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported value]"


class TraceEvent(FixtureRecord):
    id: str
    sequence: int
    time: datetime
    elapsed_ms: float
    kind: EventKind
    model_call_id: str | None = None
    operation_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    sanitized: bool = False
    data: dict[str, Any]


class TraceDocument(FixtureRecord):
    schema_version: Literal["1"] = "1"
    run_id: str
    scenario_id: str
    scenario_revision: str
    fixture_revision: str
    actor_id: str
    profile: Literal["baseline", "protected"]
    events: tuple[TraceEvent, ...]


class TraceRecorder:
    def __init__(self, prepared: PreparedScenario, profile: str):
        self.prepared = prepared
        self.profile = profile
        self._events: list[TraceEvent] = []
        self._operation: ContextVar[dict | None] = ContextVar("trace_operation", default=None)
        self._started = perf_counter()
        self._model_calls = 0
        self._operations = 0
        self._finished = False
        self._run_invoked = False
        self.record("run_started", {})
        self.snapshot("initial")

    def record(self, kind: EventKind, data: dict, **links) -> TraceEvent:
        if self._finished:
            raise ValueError("The run journal is already finalized.")
        sequence = len(self._events) + 1
        cleaned = sanitize(data)
        cleaned_links = sanitize(links)
        event = TraceEvent(
            id=f"{self.prepared.run_id}:event:{sequence}", sequence=sequence,
            time=datetime.now(UTC), elapsed_ms=round((perf_counter() - self._started) * 1000, 3),
            kind=kind, data=cleaned, sanitized=cleaned != data or cleaned_links != links, **cleaned_links,
        )
        self._events.append(event)
        return event

    def begin_run(self, question: str) -> None:
        if self._run_invoked or self._model_calls or self._finished:
            raise ValueError("Prepare a fresh Target session for each run.")
        self._run_invoked = True
        self.record("run_input", {"question": question})

    def snapshot(self, phase: str, **links) -> None:
        self.record("state_snapshot", {
            "phase": phase, "state": self.prepared.state.model_dump(mode="json"),
        }, **links)

    def start_model(self, model: Any) -> str:
        self._model_calls += 1
        call_id = f"{self.prepared.run_id}:model:{self._model_calls}"
        name = getattr(model, "model_name", None)
        self.record("model_started", {
            "adapter": type(model).__name__, "model": name if isinstance(name, str) else None,
        }, model_call_id=call_id)
        return call_id

    def policy(self, decision: str, reason: str) -> None:
        context = self._operation.get()
        if context is not None:
            self.record("policy_decision", {"decision": decision, "reason": reason}, **context)

    def validated(self, arguments: dict) -> None:
        self.record("arguments_validated", {"arguments": arguments}, **self._operation.get())

    def result(self, result: Any) -> None:
        self.record("tool_result", {"result": result, "outcome": "executed"}, **self._operation.get())

    @contextmanager
    def operation(self, model_call_id: str | None, call: dict):
        self._operations += 1
        links = {
            "model_call_id": model_call_id, "operation_id": f"{self.prepared.run_id}:op:{self._operations}",
            "tool_call_id": call.get("id"), "tool_name": call.get("name"),
        }
        token = self._operation.set(links)
        self.record("tool_attempted", {"arguments": call.get("args", {})}, **links)
        self.snapshot("before", **links)
        try:
            yield
        except Exception as error:
            code = "tool_failure"
            outcome = "failed"
            if isinstance(error, ValidationError):
                code = "invalid_arguments"
            elif isinstance(error, ToolException):
                try:
                    code = json.loads(str(error))["error"]["code"]
                except (ValueError, KeyError, TypeError):
                    code = "tool_failure"
                if not isinstance(code, str):
                    code = "tool_failure"
                if code in {"ticket_access_denied", "approval_required", "tool_budget_exhausted"}:
                    outcome = "blocked"
            self.record("tool_failed", {
                "code": code, "outcome": outcome, "error_type": type(error).__name__,
            }, **links)
            raise
        finally:
            self.snapshot("after", **links)
            self._operation.reset(token)

    def reject(self, model_call_id: str | None, calls: list[dict], code: str) -> None:
        for call in calls:
            with self.operation(model_call_id, call):
                self.policy("denied", code)
                self.record("dispatch_rejected", {"code": code, "outcome": "blocked"}, **self._operation.get())

    def finish(self, outcome: Literal["completed", "failed"], error_type: str | None = None) -> None:
        if not self._finished:
            self.snapshot("final")
            self.record("run_finished", {"outcome": outcome, "error_type": error_type})
            self._finished = True

    def document(self) -> TraceDocument:
        scenario = self.prepared.scenario
        return TraceDocument(
            run_id=self.prepared.run_id, scenario_id=scenario.id, scenario_revision=scenario.revision,
            fixture_revision=scenario.fixture_revision, actor_id=scenario.actor.id, profile=self.profile,
            events=tuple(event.model_copy(deep=True) for event in self._events),
        )

    def write(self, path: Path) -> None:
        """Atomically export observations through the trusted CLI."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(self.document().model_dump_json(indent=2))
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
