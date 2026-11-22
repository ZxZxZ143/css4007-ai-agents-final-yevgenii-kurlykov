import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException
from pydantic import ValidationError

from agentshield import cli
from agentshield.fixtures import prepare_scenario
from agentshield.graph import GraphResponseError
from agentshield.helpdesk import CreationApproval, HelpdeskSession, MAX_TOOL_CALLS
from agentshield.target import build_target_graph


def approval_for(prepared, **changes):
    fields = {
        "run_id": prepared.run_id, "actor_id": prepared.scenario.actor.id,
        "subject": "New synthetic issue", "expires_at": datetime.now(UTC) + timedelta(minutes=1),
    }
    return CreationApproval(**{**fields, **changes})


def session_tools(session):
    return {tool.name: tool for tool in session.tools()}


@pytest.mark.parametrize("profile", ["baseline", "protected"])
def test_ordinary_tools_work_in_both_profiles(profile):
    prepared = prepare_scenario("S1")
    session = HelpdeskSession(prepared, profile, (approval_for(prepared),))
    tools = session_tools(session)
    before = session.state.model_copy(deep=True)
    assert tools["get_ticket_status"].invoke({"ticket_id": "T-S1-001"}) == {
        "ticket_id": "T-S1-001", "status": "in_progress",
    }
    articles = tools["search_knowledge_base"].invoke({"query": "recovery"})["articles"]
    assert [article["id"] for article in articles] == ["KB-001"]
    assert session.state == before
    created = tools["create_support_ticket"].invoke({"subject": "New synthetic issue"})
    assert created == {"ticket_id": "T-S1-003", "status": "open"}
    assert session.state.tickets[created["ticket_id"]].customer_id == prepared.scenario.actor.id
    assert tools["get_ticket_status"].invoke({"ticket_id": created["ticket_id"]}) == created


def test_protected_denies_foreign_ticket_while_baseline_can_read_registered_data():
    protected = HelpdeskSession(prepare_scenario("S1"), "protected")
    before = protected.state.model_copy(deep=True)
    with pytest.raises(ToolException, match="ticket_access_denied"):
        session_tools(protected)["get_ticket_status"].invoke({"ticket_id": "T-S1-002"})
    assert protected.state == before
    baseline = HelpdeskSession(prepare_scenario("S1"), "baseline")
    assert session_tools(baseline)["get_ticket_status"].invoke({"ticket_id": "T-S1-002"})["status"] == "resolved"


@pytest.mark.parametrize("ticket_id,owner", [
    ("T-S1-001", "customer-other"), ("T-S1-002", "customer-s1"),
])
def test_protected_requires_both_scope_and_ownership(ticket_id, owner):
    session = HelpdeskSession(prepare_scenario("S1"), "protected")
    session.state.tickets[ticket_id] = session.state.tickets[ticket_id].model_copy(
        update={"customer_id": owner}
    )
    with pytest.raises(ToolException, match="ticket_access_denied"):
        session_tools(session)["get_ticket_status"].invoke({"ticket_id": ticket_id})


@pytest.mark.parametrize("changes", [
    {"run_id": "foreign-run"}, {"actor_id": "another-user"}, {"subject": "Different subject"},
    {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
])
def test_protected_rejects_mismatched_or_expired_approval(changes):
    prepared = prepare_scenario("S1")
    session = HelpdeskSession(prepared, "protected", (approval_for(prepared, **changes),))
    before = session.state.model_copy(deep=True)
    with pytest.raises(ToolException, match="approval_required"):
        session_tools(session)["create_support_ticket"].invoke({"subject": "New synthetic issue"})
    assert session.state == before


def test_approval_is_required_and_consumed_once():
    prepared = prepare_scenario("S1")
    session = HelpdeskSession(prepared, "protected")
    create = session_tools(session)["create_support_ticket"]
    with pytest.raises(ToolException, match="approval_required"):
        create.invoke({"subject": "New synthetic issue"})
    assert len(session.state.tickets) == 2
    session.approvals.append(approval_for(prepared))
    create.invoke({"subject": "New synthetic issue"})
    with pytest.raises(ToolException, match="approval_required"):
        create.invoke({"subject": "New synthetic issue"})
    assert len(session.state.tickets) == 3


@pytest.mark.parametrize("profile", ["baseline", "protected"])
def test_containment_and_argument_validation(profile):
    session = HelpdeskSession(prepare_scenario("S1"), profile)
    tools = session_tools(session)
    with pytest.raises(ToolException, match="ticket_not_found"):
        tools["get_ticket_status"].invoke({"ticket_id": "T-S1-999"})
    for name, args in [
        ("get_ticket_status", {"ticket_id": "../../etc/passwd"}),
        ("get_ticket_status", {"ticket_id": "T-S1-001", "actor_id": "another-user"}),
        ("search_knowledge_base", {"query": " "}),
        ("create_support_ticket", {"subject": "issue", "approved": True}),
        ("create_support_ticket", {"subject": "x" * 121}),
    ]:
        with pytest.raises(ValidationError):
            tools[name].invoke(args)
    assert len(session.state.tickets) == 2
    assert session.tool_calls == 1


@pytest.mark.parametrize("profile", ["baseline", "protected"])
def test_tool_budget_is_shared_across_tools(profile):
    session = HelpdeskSession(prepare_scenario("S1"), profile)
    tools = session_tools(session)
    for _ in range(MAX_TOOL_CALLS):
        tools["search_knowledge_base"].invoke({"query": "ticket"})
    with pytest.raises(ToolException, match="tool_budget_exhausted"):
        tools["get_ticket_status"].invoke({"ticket_id": "T-S1-001"})
    assert session.tool_calls == MAX_TOOL_CALLS


def test_new_session_does_not_share_mock_effects():
    first = HelpdeskSession(prepare_scenario("S1"), "baseline")
    session_tools(first)["create_support_ticket"].invoke({"subject": "New synthetic issue"})
    second = HelpdeskSession(prepare_scenario("S1"), "baseline")
    assert len(first.state.tickets) == 3
    assert len(second.state.tickets) == 2
    with pytest.raises(ValueError, match="fresh registered"):
        HelpdeskSession(prepare_scenario("S1").model_copy(update={"state": first.state}), "protected")


@pytest.mark.parametrize("profile", ["baseline", "protected"])
def test_target_graph_uses_real_status_tool_with_mock_model(profile):
    prepared = prepare_scenario("S1")
    session = HelpdeskSession(prepared, profile)
    model = Mock()
    answer = {"ticket_id": "T-S1-001", "status": "in_progress", "lookup_call_id": "status-1"}
    model.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "get_ticket_status", "args": {"ticket_id": "T-S1-001"}, "id": "status-1",
        }]), AIMessage(content=json.dumps(answer)),
    ]
    messages = build_target_graph(model, session).invoke({
        "messages": [HumanMessage(content=prepared.scenario.goal)]
    })["messages"]
    received = model.bind_tools.return_value.invoke.call_args.args[0]
    assert "oracle" not in received[0].text
    assert prepared.scenario.tickets[0].subject not in received[0].text
    assert isinstance(received[-1], ToolMessage)
    assert received[-1].tool_call_id == "status-1"
    assert json.loads(received[-1].content) == {"ticket_id": "T-S1-001", "status": "in_progress"}
    assert json.loads(messages[-1].text) == answer
    assert session.tool_calls == 1
    assert [tool.name for tool in model.bind_tools.call_args.args[0]] == [
        "search_knowledge_base", "get_ticket_status", "create_support_ticket",
    ]


def test_target_graph_returns_policy_denial_to_model_without_mock_write():
    session = HelpdeskSession(prepare_scenario("S1"), "protected")
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "create_support_ticket", "args": {"subject": "issue"}, "id": "create-1",
        }]), AIMessage(content="Approval is required."),
    ]
    build_target_graph(model, session).invoke({"messages": [HumanMessage("Create a ticket")]})
    message = model.bind_tools.return_value.invoke.call_args.args[0][-1]
    assert message.status == "error"
    assert json.loads(message.content)["error"]["code"] == "approval_required"
    assert len(session.state.tickets) == 2


@pytest.mark.parametrize("tool_name", ["shell", "list_scenarios"])
def test_target_graph_denies_unregistered_tools(tool_name):
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = Mock()
    model.bind_tools.return_value.invoke.return_value = AIMessage(content="", tool_calls=[{
        "name": tool_name, "args": {}, "id": "unsupported-1",
    }])
    with pytest.raises(GraphResponseError, match="unsupported"):
        build_target_graph(model, session).invoke({"messages": [HumanMessage("test")]})
    assert session.tool_calls == 0


def test_target_graph_does_not_execute_repeated_call_id():
    session = HelpdeskSession(prepare_scenario("S1"), "baseline")
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = lambda _: AIMessage(content="", tool_calls=[{
        "name": "create_support_ticket", "args": {"subject": "issue"}, "id": "same-id",
    }])
    with pytest.raises(GraphResponseError, match="reused"):
        build_target_graph(model, session).invoke({"messages": [HumanMessage("Create a ticket")]})
    assert len(session.state.tickets) == 3


def test_target_cli_default_goal_with_mock_model(monkeypatch, capsys, tmp_path):
    model = Mock()
    model.bind_tools.return_value.invoke.return_value = AIMessage(content="Synthetic answer")
    monkeypatch.setattr(cli, "create_model", lambda: model)
    assert cli.main(["target", "--profile", "protected", "--trace-file", str(tmp_path / "run.json")]) == 0
    assert "Synthetic answer" in capsys.readouterr().out
    assert model.bind_tools.return_value.invoke.call_args.args[0][-1].text == prepare_scenario("S1").scenario.goal


@pytest.mark.parametrize("approved", [False, True])
def test_target_cli_explicit_creation_approval(monkeypatch, capsys, approved, tmp_path):
    model = Mock()
    model.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "create_support_ticket", "args": {"subject": "New synthetic issue"}, "id": "create-1",
        }]), AIMessage(content="Finished processing the tool result."),
    ]
    monkeypatch.setattr(cli, "create_model", lambda: model)
    args = ["target", "Create a ticket", "--verbose", "--trace-file", str(tmp_path / "run.json")]
    if approved:
        args += ["--approve-subject", "New synthetic issue"]
    assert cli.main(args) == 0
    received = model.bind_tools.return_value.invoke.call_args.args[0][-1]
    assert received.status == ("success" if approved else "error")
    output = capsys.readouterr().out
    expected = "T-S1-003" if approved else "approval_required"
    assert expected in output
    if not approved:
        assert "0 tool execution(s)" in output
        assert "1 unsuccessful tool call(s)" in output
