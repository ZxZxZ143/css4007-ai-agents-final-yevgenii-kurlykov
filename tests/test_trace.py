from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from agentshield import cli
from agentshield.config import ConfigurationError
from agentshield.evaluation import LookupEvidence, TicketAnswer, evaluate_s1
from agentshield.fixtures import HelpdeskState, prepare_scenario
from agentshield.graph import GraphResponseError
from agentshield.helpdesk import HelpdeskSession, MAX_TOOL_CALLS
from agentshield.target import run_target
from agentshield.trace import TraceDocument, TraceRecorder


def model_for_call(name, arguments, answer="Finished."):
    model = Mock()
    model.model_name = "offline-mock"
    model.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": "call-1"}],
                  response_metadata={"id": "mock-response-1"}),
        AIMessage(content=answer, response_metadata={"id": "mock-response-2"}),
    ]
    return model


def snapshots(document, phase):
    return [event for event in document.events
            if event.kind == "state_snapshot" and event.data["phase"] == phase]


@pytest.mark.parametrize("profile", ["baseline", "protected"])
def test_s1_trace_joins_model_tool_and_snapshots_and_supports_independent_check(profile, tmp_path):
    prepared = prepare_scenario("S1")
    session = HelpdeskSession(prepared, profile)
    answer = TicketAnswer(ticket_id="T-S1-001", status="in_progress", lookup_call_id="call-1")
    model = model_for_call("get_ticket_status", {"ticket_id": "T-S1-001"}, answer.model_dump_json())
    run_target(model, session, prepared.scenario.goal)
    path = tmp_path / "trace.json"
    session.trace.write(path)
    document = TraceDocument.model_validate_json(path.read_text(encoding="utf-8"))
    assert document.fixture_revision == "helpdesk-s1-v1"
    assert document.profile == profile
    assert [event.sequence for event in document.events] == list(range(1, len(document.events) + 1))
    assert len({event.id for event in document.events}) == len(document.events)
    attempted = next(event for event in document.events if event.kind == "tool_attempted")
    operation = [event for event in document.events if event.operation_id == attempted.operation_id]
    assert {event.kind for event in operation} == {
        "tool_attempted", "state_snapshot", "arguments_validated", "policy_decision", "tool_result",
    }
    assert all(event.model_call_id == attempted.model_call_id and event.tool_call_id == "call-1"
               for event in operation)
    model_response = next(event for event in document.events
                          if event.kind == "model_response" and event.model_call_id == attempted.model_call_id)
    assert model_response.data["tool_calls"] == [{"id": "call-1", "name": "get_ticket_status"}]
    validated = next(event for event in operation if event.kind == "arguments_validated")
    returned = next(event for event in operation if event.kind == "tool_result")
    lookup = LookupEvidence(
        run_id=document.run_id, fixture_revision=document.fixture_revision,
        call_id=returned.tool_call_id, actor_id=document.actor_id,
        requested_ticket_id=validated.data["arguments"]["ticket_id"],
        returned_ticket_id=returned.data["result"]["ticket_id"],
        returned_status=returned.data["result"]["status"],
    )
    final_answer = [event for event in document.events if event.kind == "model_response"][-1].data["text"]
    final_state = HelpdeskState.model_validate(snapshots(document, "final")[0].data["state"])
    assert evaluate_s1(prepared, lookup, TicketAnswer.model_validate_json(final_answer), final_state).outcome == "passed"
    assert snapshots(document, "before")[0].data["state"] == snapshots(document, "after")[0].data["state"]
    assert document.events[-1].data["outcome"] == "completed"


def test_creation_trace_records_normalized_arguments_and_detached_effect_snapshots():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = model_for_call("create_support_ticket", {"subject": "  Synthetic issue  "})
    run_target(model, session, "Create a ticket")
    document = session.trace.document()
    validated = next(event for event in document.events if event.kind == "arguments_validated")
    assert validated.data["arguments"] == {"subject": "Synthetic issue"}
    before = snapshots(document, "before")[0].data["state"]["tickets"]
    after = snapshots(document, "after")[0].data["state"]["tickets"]
    assert len(before) == 2 and len(after) == 3
    assert after["T-S1-003"]["subject"] == "Synthetic issue"
    session.state.tickets.clear()
    assert len(snapshots(session.trace.document(), "final")[0].data["state"]["tickets"]) == 3
    document.events[0].data["modified_export"] = True
    assert "modified_export" not in session.trace.document().events[0].data


def test_denied_write_records_block_and_unchanged_state():
    session = HelpdeskSession(prepare_scenario("S1"), "protected")
    model = model_for_call("create_support_ticket", {"subject": "Synthetic issue"})
    run_target(model, session, "Create a ticket")
    document = session.trace.document()
    decision = next(event for event in document.events if event.kind == "policy_decision")
    failure = next(event for event in document.events if event.kind == "tool_failed")
    assert decision.data == {"decision": "denied", "reason": "approval_required"}
    assert failure.data["outcome"] == "blocked"
    assert failure.operation_id == decision.operation_id
    assert not any(event.kind == "tool_result" for event in document.events)
    assert snapshots(document, "before")[0].data["state"] == snapshots(document, "after")[0].data["state"]


def test_invalid_arguments_are_recorded_before_handler_and_finish_as_failure():
    session = HelpdeskSession(prepare_scenario("S1"), "protected")
    model = model_for_call("create_support_ticket", {"subject": "issue", "approved": True})
    with pytest.raises(GraphResponseError, match="invalid tool arguments"):
        run_target(model, session, "Create a ticket")
    document = session.trace.document()
    assert session.tool_calls == 0
    assert not any(event.kind == "arguments_validated" for event in document.events)
    failure = next(event for event in document.events if event.kind == "tool_failed")
    assert failure.data["code"] == "invalid_arguments"
    assert document.events[-1].data["outcome"] == "failed"
    assert len(snapshots(document, "final")) == 1


def test_unregistered_tool_attempt_is_recorded_without_execution():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    with pytest.raises(GraphResponseError, match="unsupported"):
        run_target(model_for_call("shell", {"command": "untrusted"}), session, "test")
    document = session.trace.document()
    rejected = next(event for event in document.events if event.kind == "dispatch_rejected")
    assert rejected.tool_name == "shell"
    assert rejected.data["outcome"] == "blocked"
    assert session.tool_calls == 0


def test_tool_failure_preserves_partial_effect_and_hides_exception_text(monkeypatch):
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    tools = session.tools()

    def failing_search(query):
        session.state.tickets.pop("T-S1-002")
        raise RuntimeError("sensitive exception text")

    tools[0].func = failing_search
    monkeypatch.setattr(session, "tools", lambda: tools)
    with pytest.raises(RuntimeError):
        run_target(model_for_call("search_knowledge_base", {"query": "ticket"}), session, "Search")
    document = session.trace.document()
    assert "sensitive exception text" not in document.model_dump_json()
    assert len(snapshots(document, "before")[0].data["state"]["tickets"]) == 2
    assert len(snapshots(document, "after")[0].data["state"]["tickets"]) == 1
    assert document.events[-1].data == {"outcome": "failed", "error_type": "RuntimeError"}


def test_model_failure_is_saved_by_cli_without_exception_contents(monkeypatch, tmp_path, capsys):
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = TimeoutError("sensitive exception text")
    monkeypatch.setattr(cli, "create_model", lambda: model)
    path = tmp_path / "failed.json"
    assert cli.main(["target", "--trace-file", str(path)]) == 1
    document = TraceDocument.model_validate_json(path.read_text(encoding="utf-8"))
    assert any(event.kind == "model_failed" for event in document.events)
    assert document.events[-1].data["outcome"] == "failed"
    assert len(snapshots(document, "final")) == 1
    captured = capsys.readouterr()
    assert "sensitive exception text" not in captured.out + captured.err + document.model_dump_json()


def test_export_excludes_reasoning_metadata_and_redacts_credentials(tmp_path):
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    credential = "sk-" + "syntheticCredential" * 2
    model = model_for_call("create_support_ticket", {"subject": credential}, credential)
    # Use an explicit list to include private content blocks in the mocked model response.
    model.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "id": "call-1", "name": "create_support_ticket", "args": {"subject": credential},
        }]),
        AIMessage(content=[{"type": "reasoning", "encrypted_content": "private-reasoning"},
                           {"type": "text", "text": credential}],
                  response_metadata={"untrusted_metadata": "private-metadata"}),
    ]
    run_target(model, session, "test")
    path = tmp_path / "trace.json"
    session.trace.write(path)
    saved = path.read_text(encoding="utf-8")
    assert credential not in saved
    assert "private-reasoning" not in saved and "private-metadata" not in saved
    assert "[redacted]" in saved
    assert any(event.sanitized for event in session.trace.document().events)
    assert set(tmp_path.iterdir()) == {path}


def test_budget_and_graph_limit_leave_final_trace():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = Mock()
    counter = iter(range(100))
    model.bind_tools.return_value.invoke.side_effect = lambda _: AIMessage(content="", tool_calls=[{
        "id": f"call-{next(counter)}", "name": "get_ticket_status", "args": {"ticket_id": "T-S1-001"},
    }])
    with pytest.raises(GraphRecursionError):
        run_target(model, session, "test")
    document = session.trace.document()
    assert session.tool_calls == MAX_TOOL_CALLS
    assert any(event.kind == "tool_failed" and event.data["code"] == "tool_budget_exhausted"
               for event in document.events)
    assert document.events[-1].data["error_type"] == "GraphRecursionError"
    assert len(snapshots(document, "final")) == 1


def test_session_cannot_be_reused_after_finishing():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = model_for_call("get_ticket_status", {"ticket_id": "T-S1-001"})
    run_target(model, session, "test")
    previous = session.trace.document()
    with pytest.raises(ValueError, match="fresh Target session"):
        run_target(model, session, "test again")
    assert session.trace.document() == previous


def test_cli_journal_write_failure_is_controlled(monkeypatch, tmp_path, capsys):
    model = Mock()
    model.bind_tools.return_value.invoke.return_value = AIMessage(content="Done.")
    monkeypatch.setattr(cli, "create_model", lambda: model)

    def fail_write(self, path):
        raise PermissionError("private path detail")

    monkeypatch.setattr(TraceRecorder, "write", fail_write)
    assert cli.main(["target", "--trace-file", str(tmp_path / "run.json")]) == 1
    assert "could not save the run journal" in capsys.readouterr().err


def test_default_cli_path_preserves_configuration_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def missing_key():
        raise ConfigurationError("Set OPENAI_API_KEY.")

    monkeypatch.setattr(cli, "create_model", missing_key)
    assert cli.main(["target"]) == 1
    paths = list((tmp_path / ".private" / "traces").glob("*.json"))
    assert len(paths) == 1
    document = TraceDocument.model_validate_json(paths[0].read_text(encoding="utf-8"))
    assert not any(event.kind == "model_started" for event in document.events)
    assert document.events[-1].data == {"outcome": "failed", "error_type": "ConfigurationError"}


def test_repeated_call_id_has_distinct_attempts_without_duplicate_write():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = lambda _: AIMessage(content="", tool_calls=[{
        "id": "same-call", "name": "create_support_ticket", "args": {"subject": "Synthetic issue"},
    }])
    with pytest.raises(GraphResponseError, match="reused"):
        run_target(model, session, "Create a ticket")
    document = session.trace.document()
    attempts = [event for event in document.events if event.kind == "tool_attempted"]
    assert len(attempts) == 2
    assert attempts[0].operation_id != attempts[1].operation_id
    assert attempts[0].tool_call_id == attempts[1].tool_call_id == "same-call"
    assert len(session.state.tickets) == 3
    assert sum(event.kind == "tool_result" for event in document.events) == 1


def test_atomic_export_keeps_previous_file_on_replace_failure(monkeypatch, tmp_path):
    recorder = HelpdeskSession(prepare_scenario("S1"), "baseline").trace
    path = tmp_path / "trace.json"
    path.write_text("previous contents", encoding="utf-8")

    def fail_replace(source, destination):
        raise PermissionError("test replacement failure")

    monkeypatch.setattr("agentshield.trace.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        recorder.write(path)
    assert path.read_text(encoding="utf-8") == "previous contents"
    assert set(tmp_path.iterdir()) == {path}


def test_interrupted_cli_run_saves_final_snapshot(monkeypatch, tmp_path):
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(cli, "create_model", lambda: model)
    path = tmp_path / "interrupted.json"
    assert cli.main(["target", "--trace-file", str(path)]) == 130
    document = TraceDocument.model_validate_json(path.read_text(encoding="utf-8"))
    assert document.events[-1].data == {"outcome": "failed", "error_type": "KeyboardInterrupt"}
    assert len(snapshots(document, "final")) == 1
