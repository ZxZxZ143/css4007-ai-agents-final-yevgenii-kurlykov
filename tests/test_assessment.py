import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from agentshield import cli
from agentshield.assessment import MAX_TRACE_BYTES, evaluate_s1_trace, evaluate_trace_file
from agentshield.fixtures import S1, prepare_scenario
from agentshield.helpdesk import HelpdeskSession
from agentshield.s1 import run_s1_pair
from agentshield.target import run_target
from agentshield.trace import TraceDocument


def tool_response(name="get_ticket_status", arguments=None):
    return AIMessage(content="", tool_calls=[{
        "id": "call-1", "name": name,
        "args": arguments if arguments is not None else {"ticket_id": "T-S1-001"},
    }])


def final_response(status="in_progress"):
    return AIMessage(content=json.dumps({
        "ticket_id": "T-S1-001", "status": status, "lookup_call_id": "call-1",
    }))


@pytest.fixture
def trace():
    prepared = prepare_scenario("S1")
    session = HelpdeskSession(prepared, "protected")
    model = Mock()
    model.model_name = "offline-mock"
    model.bind_tools.return_value.invoke.side_effect = [tool_response(), final_response()]
    run_target(model, session, S1.goal)
    return session.trace.document()


def test_saved_trace_is_independently_evaluated_and_links_evidence(trace, tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(trace.model_dump_json(), encoding="utf-8")
    result = evaluate_trace_file(path)
    assert result.outcome == "passed"
    assert result.profile == "protected"
    assert result.run_id == trace.run_id
    assert result.evidence_ids == tuple(event.id for event in trace.events)
    assert result.failures == result.missing_evidence == ()


@pytest.mark.parametrize("missing", [
    "tool_result", "arguments_validated", "policy_decision", "run_finished",
    "initial", "final", "after", "final_answer",
])
def test_missing_evidence_never_passes(trace, missing):
    retained = []
    for event in trace.events:
        remove = event.kind == missing or (
            event.kind == "state_snapshot" and event.data.get("phase") == missing
        ) or (missing == "final_answer" and event.kind == "model_response" and not event.data["tool_calls"])
        if not remove:
            retained.append(event)
    result = evaluate_s1_trace(trace.model_copy(update={"events": tuple(retained)}))
    assert result.outcome == "inconclusive"
    assert result.missing_evidence


def test_wrong_final_answer_is_failed_even_when_final_snapshot_is_missing(trace):
    events = []
    for event in trace.events:
        if event.kind == "state_snapshot" and event.data.get("phase") == "final":
            continue
        if event.kind == "model_response" and not event.data["tool_calls"]:
            event = event.model_copy(update={"data": {**event.data, "text": final_response("resolved").text}})
        events.append(event)
    result = evaluate_s1_trace(trace.model_copy(update={"events": tuple(events)}))
    assert result.outcome == "failed"
    assert any("incorrect" in reason for reason in result.failures)
    assert result.missing_evidence


@pytest.mark.parametrize("text", ["Not JSON", '{"passed":true}', '{"ticket_id":"T-S1-001","status":"invented"}'])
def test_malformed_final_response_is_task_failure(trace, text):
    events = tuple(event.model_copy(update={"data": {**event.data, "text": text}})
                   if event.kind == "model_response" and not event.data["tool_calls"] else event
                   for event in trace.events)
    result = evaluate_s1_trace(trace.model_copy(update={"events": events}))
    assert result.outcome == "failed"
    assert any("TicketAnswer JSON" in reason for reason in result.failures)


@pytest.mark.parametrize("field,value", [
    ("tool_call_id", "another-call"), ("operation_id", "another-run:op:1"),
    ("model_call_id", "another-run:model:1"), ("sanitized", True),
])
def test_broken_lookup_correlations_are_inconclusive(trace, field, value):
    events = tuple(event.model_copy(update={field: value}) if event.kind == "tool_result" else event
                   for event in trace.events)
    assert evaluate_s1_trace(trace.model_copy(update={"events": events})).outcome == "inconclusive"


@pytest.mark.parametrize("field,value", [
    ("run_id", "foreign-run"), ("fixture_revision", "future-fixture"),
    ("scenario_revision", "future-scenario"), ("actor_id", "foreign-user"),
])
def test_foreign_context_is_inconclusive(trace, field, value):
    assert evaluate_s1_trace(trace.model_copy(update={field: value})).outcome == "inconclusive"


def test_changed_then_restored_state_is_detected(trace):
    events = []
    for event in trace.events:
        if event.kind == "state_snapshot" and event.data.get("phase") == "after":
            data = json.loads(json.dumps(event.data))
            data["state"]["tickets"]["T-S1-001"]["status"] = "resolved"
            event = event.model_copy(update={"data": data})
        events.append(event)
    assert evaluate_s1_trace(trace.model_copy(update={"events": tuple(events)})).outcome == "failed"


def test_different_task_is_outside_s1_assessment(trace):
    events = tuple(event.model_copy(update={"data": {"question": "Create a ticket"}})
                   if event.kind == "run_input" else event for event in trace.events)
    assert evaluate_s1_trace(trace.model_copy(update={"events": events})).outcome == "inconclusive"


def test_duplicate_result_is_not_silently_selected(trace):
    result = next(event for event in trace.events if event.kind == "tool_result")
    duplicated = trace.model_copy(update={"events": (*trace.events, result)})
    assert evaluate_s1_trace(duplicated).outcome == "inconclusive"


def test_correct_answer_without_lookup_remains_inconclusive():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = Mock()
    model.bind_tools.return_value.invoke.return_value = final_response()
    run_target(model, session, S1.goal)
    assert evaluate_s1_trace(session.trace.document()).outcome == "inconclusive"


def test_wrong_registered_ticket_lookup_fails_despite_correct_final_text():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = [
        tool_response(arguments={"ticket_id": "T-S1-002"}), final_response(),
    ]
    run_target(model, session, S1.goal)
    assert evaluate_s1_trace(session.trace.document()).outcome == "failed"


def test_paired_runs_use_same_goal_config_and_fresh_fixture(tmp_path):
    model = Mock()
    model.model_name = "offline-mock"
    model.bind_tools.return_value.invoke.side_effect = [
        tool_response(), final_response(), tool_response(), final_response(),
    ]
    report = run_s1_pair(model, tmp_path)
    assert [run.profile for run in report.runs] == ["baseline", "protected"]
    assert [run.evaluation.outcome for run in report.runs] == ["passed", "passed"]
    assert report.runs[0].evaluation.run_id != report.runs[1].evaluation.run_id
    documents = [TraceDocument.model_validate_json(Path(run.trace_path).read_text(encoding="utf-8"))
                 for run in report.runs]
    initial = [next(event.data["state"] for event in doc.events
                    if event.kind == "state_snapshot" and event.data.get("phase") == "initial")
               for doc in documents]
    assert initial[0] == initial[1]
    assert all(doc.fixture_revision == S1.fixture_revision for doc in documents)
    calls = model.bind_tools.return_value.invoke.call_args_list
    assert calls[0].args[0][-1].text == calls[2].args[0][-1].text == S1.goal
    assert calls[0].args[0][0] == calls[2].args[0][0]
    assert model.bind_tools.call_count == 2
    assert all(evaluate_trace_file(Path(run.trace_path)).outcome == "passed" for run in report.runs)
    saved = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert saved["model_adapter"] == "Mock" and saved["model"] == "offline-mock"


def test_first_profile_failure_does_not_hide_second_profile_result(tmp_path):
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = [TimeoutError("private error"), tool_response(), final_response()]
    report = run_s1_pair(model, tmp_path)
    assert [run.evaluation.outcome for run in report.runs] == ["failed", "passed"]
    assert "private error" not in (tmp_path / "evaluation.json").read_text(encoding="utf-8")


def test_first_profile_mock_write_does_not_pollute_second_fixture(tmp_path):
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = [
        tool_response("create_support_ticket", {"subject": "Unexpected mutation"}), final_response(),
        tool_response(), final_response(),
    ]
    report = run_s1_pair(model, tmp_path)
    assert [run.evaluation.outcome for run in report.runs] == ["failed", "passed"]


def test_s1_cli_runs_both_profiles_and_saves_report(monkeypatch, tmp_path, capsys):
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = [
        tool_response(), final_response(), tool_response(), final_response(),
    ]
    factory = Mock(return_value=model)
    monkeypatch.setattr(cli, "create_model", factory)
    assert cli.main(["s1", "--output-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "baseline = passed" in output and "protected = passed" in output
    assert (tmp_path / "evaluation.json").is_file()
    factory.assert_called_once_with()


def test_evaluate_cli_is_offline(trace, monkeypatch, tmp_path, capsys):
    factory = Mock(side_effect=AssertionError("OpenAI must not be called"))
    monkeypatch.setattr(cli, "create_model", factory)
    path = tmp_path / "trace.json"
    path.write_text(trace.model_dump_json(), encoding="utf-8")
    assert cli.main(["evaluate", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "passed"
    factory.assert_not_called()


@pytest.mark.parametrize("content", ["invalid JSON", "x" * (MAX_TRACE_BYTES + 1)])
def test_invalid_trace_files_have_controlled_cli_errors(tmp_path, capsys, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    assert cli.main(["evaluate", str(path)]) == 2
    assert "could not read a valid journal" in capsys.readouterr().err
