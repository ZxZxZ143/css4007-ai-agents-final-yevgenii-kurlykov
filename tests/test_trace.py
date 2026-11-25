import json
from datetime import timedelta
from pathlib import Path

import pytest

from deskpilot.redaction import MAX_LINE_BYTES
from deskpilot.trace import EventKind, Outcome, TraceRecorder, operation_chain, read_trace

SECRET = "sk-proj-abcdefghijklmnop1234567890"


def test_operation_chain_correlation(tmp_path: Path):
    path = tmp_path / "t" / "run.jsonl"
    with TraceRecorder(path) as rec:
        rec.start_run("open notepad")
        op = rec.new_operation()
        m = rec.emit(EventKind.MODEL_CALL, {"model": "m"}, operation_id=op)
        t = rec.emit(EventKind.TOOL_CALL, {"tool": "open"}, operation_id=op, parent_id=m)
        p = rec.emit(EventKind.POLICY_DECISION, {"allow": True}, operation_id=op, parent_id=t)
        rec.emit(EventKind.OBSERVATION, {"x": 1})
        r = rec.emit(EventKind.TOOL_RESULT, {"ok": True}, operation_id=op, parent_id=p)
        rec.finish(Outcome.COMPLETED, "goal_met")
    events = read_trace(path)
    chain = operation_chain(events, op)
    assert [e.kind for e in chain] == [
        EventKind.MODEL_CALL,
        EventKind.TOOL_CALL,
        EventKind.POLICY_DECISION,
        EventKind.TOOL_RESULT,
    ]
    assert [e.parent_id for e in chain] == [None, m, t, p]
    assert chain[-1].event_id == r
    assert events[-1].payload["outcome"] == "completed"
    assert events[0].payload["goal"] == "open notepad"


def test_secrets_never_in_file_bytes(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    pem = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"
    with TraceRecorder(path, secrets=["hunter-two-secret"]) as rec:
        rec.start_run(f"use {SECRET}")
        rec.emit(
            EventKind.TOOL_CALL,
            {
                "headers": {"Authorization": "Bearer abcdefgh12345"},
                "items": [{"note": f"x {SECRET} y"}, "AKIAABCDEFGHIJKLMNOP", pem],
                "free": "has hunter-two-secret inside",
                "api_key": "zzzz-raw-key",
            },
        )
    data = path.read_bytes().decode()
    raws = (SECRET, "abcdefgh12345", "AKIAABCDEFGHIJKLMNOP", "AAAA", "hunter-two-secret")
    for raw in (*raws, "zzzz-raw-key"):
        assert raw not in data
    assert "[REDACTED]" in data


def test_private_reasoning_not_written(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    with TraceRecorder(path) as rec:
        rec.emit(EventKind.MODEL_RESULT, {"reasoning": "SECRET THOUGHT", "text": "hi"})
    data = path.read_text(encoding="utf-8")
    assert "SECRET THOUGHT" not in data
    assert "[OMITTED]" in data


def test_huge_payload_line_cap(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    with TraceRecorder(path) as rec:
        rec.emit(EventKind.OBSERVATION, {f"k{i}": "y" * 1500 for i in range(60)})
    line = path.read_bytes().splitlines()[0]
    assert len(line) <= MAX_LINE_BYTES
    payload = json.loads(line)["payload"]
    assert payload["truncated"] is True
    assert payload["original_bytes"] > MAX_LINE_BYTES
    assert len(payload["preview"]) <= 500


def test_non_serializable_payload(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    with TraceRecorder(path) as rec:
        rec.emit(EventKind.OBSERVATION, {"p": Path("a/b"), "o": object()})
    payload = read_trace(path)[0].payload
    assert isinstance(payload["o"], str)


def test_sequence_ids_ts_and_guards(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    rec = TraceRecorder(path, run_id="run1")
    rec.start_run("g")
    rec.emit(EventKind.OBSERVATION)
    with pytest.raises(ValueError):
        rec.emit(EventKind.OBSERVATION, parent_id="nope")
    rec.finish(Outcome.PARTIAL, "max_steps")
    with pytest.raises(RuntimeError):
        rec.emit(EventKind.OBSERVATION)
    rec.close()
    events = read_trace(path)
    assert rec.run_id == "run1"
    assert [e.seq for e in events] == [1, 2, 3]
    assert {e.run_id for e in events} == {"run1"}
    assert all(e.ts.utcoffset() == timedelta(0) for e in events)
    assert len({e.event_id for e in events}) == 3


def test_append_across_instances(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    for _ in range(2):
        with TraceRecorder(path) as rec:
            rec.start_run("g")
            rec.finish(Outcome.FAILED, "error")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert all(json.loads(line)["schema_version"] == 1 for line in lines)
    assert len({e.run_id for e in read_trace(path)}) == 2


def test_run_boundary_kinds_cannot_be_forged(tmp_path: Path):
    with TraceRecorder(tmp_path / "r.jsonl") as rec:
        with pytest.raises(ValueError):
            rec.emit(EventKind.RUN_FINISHED, {"outcome": "completed"})
        with pytest.raises(ValueError):
            rec.emit(EventKind.RUN_STARTED)
        rec.start_run("g")
        with pytest.raises(ValueError):
            rec.start_run("again")
        rec.finish(Outcome.COMPLETED, "ok")
        with pytest.raises(RuntimeError):
            rec.finish(Outcome.FAILED, "again")


def test_invalid_kind_does_not_consume_seq(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    with TraceRecorder(path) as rec:
        rec.start_run("g")
        with pytest.raises(ValueError):
            rec.emit("bogus")  # type: ignore[arg-type]
        rec.emit(EventKind.OBSERVATION)
    assert [e.seq for e in read_trace(path)] == [1, 2]


def test_run_id_reuse_rejected_and_chain_filter(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    with TraceRecorder(path, run_id="A") as rec:
        rec.emit(EventKind.MODEL_CALL, operation_id="op")
    with pytest.raises(ValueError):
        TraceRecorder(path, run_id="A")
    with TraceRecorder(path, run_id="B") as rec:
        rec.emit(EventKind.MODEL_CALL, operation_id="op")
    events = read_trace(path)
    assert len(operation_chain(events, "op")) == 2
    assert [e.run_id for e in operation_chain(events, "op", run_id="B")] == ["B"]


def test_tier_and_cache_event_kinds(tmp_path: Path):
    kinds = [
        EventKind.MODEL_ESCALATED,
        EventKind.CACHE_HIT,
        EventKind.CACHE_MISS,
        EventKind.CACHE_STORED,
        EventKind.CACHE_INVALIDATED,
    ]
    with TraceRecorder(tmp_path / "t.jsonl") as rec:
        for kind in kinds:
            rec.emit(kind, {"k": 1})
    assert [e.kind for e in read_trace(tmp_path / "t.jsonl")] == kinds
