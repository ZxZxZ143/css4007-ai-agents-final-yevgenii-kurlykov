import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from agentshield import config
from agentshield.cli import main
from agentshield.graph import build_graph
from agentshield.tools import list_scenarios


def test_catalog_filter():
    security = list_scenarios.invoke({"category": "security"})["scenarios"]
    assert [row["id"] for row in security] == ["S2"]
    assert security[0]["category"] == "security"
    all_scenarios = list_scenarios.invoke({"category": None})["scenarios"]
    assert [row["id"] for row in all_scenarios] == ["S1", "S2", "S5"]


def test_catalog_rejects_invalid_arguments():
    with pytest.raises(ValidationError):
        list_scenarios.invoke({"category": "finance"})
    with pytest.raises(ValidationError):
        list_scenarios.invoke({"category": "security", "path": "unregistered"})


def test_graph_returns_registered_tool_result_to_model():
    model = Mock()
    invoke = model.bind_tools.return_value.invoke
    invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "list_scenarios", "args": {"category": "security"}, "id": "test-call",
        }]),
        AIMessage(content="S2 — Knowledge-base prompt injection"),
    ]

    messages = build_graph(model).invoke({"messages": [HumanMessage("List security scenarios")]})["messages"]
    tool_result = invoke.call_args.args[0][-1]
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.tool_call_id == "test-call"
    assert json.loads(tool_result.content)["scenarios"][0]["id"] == "S2"
    assert messages[-1].text == "S2 — Knowledge-base prompt injection"
    model.bind_tools.assert_called_once_with([list_scenarios], strict=True, parallel_tool_calls=False)


def test_graph_stops_after_direct_answer():
    model = Mock()
    invoke = model.bind_tools.return_value.invoke
    invoke.return_value = AIMessage(content="AgentShield Lab tests synthetic Helpdesk agents.")
    messages = build_graph(model).invoke({"messages": [HumanMessage("What is AgentShield Lab?")]})["messages"]
    assert len(messages) == 2
    assert messages[-1].text == invoke.return_value.text
    assert invoke.call_count == 1


def test_cli_missing_credentials(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    assert main(["ask", "What is AgentShield Lab?"]) == 1
    assert "Set OPENAI_API_KEY" in capsys.readouterr().err


def test_graph_stops_repeated_tool_calls():
    model = Mock()
    invoke = model.bind_tools.return_value.invoke
    invoke.side_effect = lambda messages: AIMessage(content="", tool_calls=[{
        "name": "list_scenarios", "args": {"category": None}, "id": f"test-{len(messages)}",
    }])
    with pytest.raises(GraphRecursionError):
        build_graph(model).invoke({"messages": [HumanMessage("List scenarios")]})
    assert invoke.call_count == 3
