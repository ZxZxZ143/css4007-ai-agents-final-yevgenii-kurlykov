import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import get_args

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from agentshield.config import ConfigurationError, create_model
from agentshield.fixtures import prepare_scenario
from agentshield.graph import GraphResponseError, build_graph
from agentshield.helpdesk import CreationApproval, HelpdeskSession
from agentshield.target import build_target_graph
from agentshield.tools import Category, list_scenarios


def api_error_message(error: APIError) -> str:
    if isinstance(error, AuthenticationError):
        return "OpenAI rejected OPENAI_API_KEY (HTTP 401)."
    if isinstance(error, (NotFoundError, PermissionDeniedError)):
        return f"OpenAI could not access the configured OPENAI_MODEL (HTTP {error.status_code})."
    if isinstance(error, RateLimitError):
        return "OpenAI rate limit or quota reached (HTTP 429)."
    if isinstance(error, APITimeoutError):
        return "The OpenAI request timed out."
    if isinstance(error, APIConnectionError):
        return "Could not connect to the OpenAI API."
    if isinstance(error, APIStatusError):
        code = error.code if error.code in {"model_not_found", "unsupported_parameter"} else None
        detail = f", {code}" if code else ""
        return f"OpenAI request failed (HTTP {error.status_code}{detail})."
    return "OpenAI returned an invalid response."


def print_trace(messages: Sequence[BaseMessage], requested_model: str, elapsed: float) -> None:
    print(f"Requested model = {requested_model}")
    calls = {}
    for message in messages:
        if isinstance(message, AIMessage):
            calls.update({call["id"]: call for call in message.tool_calls})
            metadata = message.response_metadata
            model = metadata.get("model_name", requested_model)
            response_id = metadata.get("id") or metadata.get("response_id") or "unavailable"
            usage = json.dumps(message.usage_metadata) if message.usage_metadata else "unavailable"
            print(f"Model response = {model}, response ID = {response_id}, tokens = {usage}")
        if isinstance(message, ToolMessage):
            call = calls[message.tool_call_id]
            print(f"Tool = {message.name}, call ID = {message.tool_call_id}, status = {message.status}")
            print(f"Arguments = {json.dumps(call['args'])}")
            print(f"Result = {message.content}")
    print(f"Elapsed = {elapsed:.2f}s")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentshield")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="Ask the LangGraph agent a question.")
    ask.add_argument("question")
    ask.add_argument("--verbose", action="store_true", help="Show model usage and catalog tool calls.")
    scenarios = commands.add_parser("scenarios", help="Read the synthetic catalog offline.")
    scenarios.add_argument("--category", choices=get_args(Category))
    target = commands.add_parser("target", help="Run the synthetic S1 Helpdesk Target.")
    target.add_argument("question", nargs="?")
    target.add_argument("--profile", choices=("baseline", "protected"), default="protected")
    target.add_argument("--verbose", action="store_true")
    target.add_argument("--approve-subject", help="Approve one exact ticket subject for 60 seconds.")
    args = parser.parse_args(argv)

    if args.command == "scenarios":
        print(json.dumps(list_scenarios.invoke({"category": args.category}), indent=2))
        return 0

    prepared = prepare_scenario("S1") if args.command == "target" else None
    question = (args.question if args.question is not None else prepared.scenario.goal).strip()
    if not question or len(question) > 4000:
        parser.error("question must contain 1–4000 characters")

    approvals = ()
    if prepared is not None and args.approve_subject is not None:
        subject = args.approve_subject.strip()
        if not subject or len(subject) > 120:
            target.error("approved subject must contain 1–120 characters")
        approvals = (CreationApproval(
            run_id=prepared.run_id, actor_id=prepared.scenario.actor.id, subject=subject,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
        ),)

    start = perf_counter()
    try:
        model = create_model()
        if prepared is not None:
            session = HelpdeskSession(prepared, args.profile, approvals)
            graph = build_target_graph(model, session)
        else:
            graph = build_graph(model)
        messages = graph.invoke({"messages": [HumanMessage(content=question)]})["messages"]
    except (ConfigurationError, GraphResponseError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except APIError as error:
        print(f"Error: {api_error_message(error)}", file=sys.stderr)
        return 1
    except GraphRecursionError:
        print("Error: the graph reached its step limit.", file=sys.stderr)
        return 1

    print(messages[-1].text)
    model_calls = sum(isinstance(message, AIMessage) for message in messages)
    tool_calls = sum(isinstance(message, ToolMessage) for message in messages)
    print(f"\nCompleted with {model_calls} model response(s) and {tool_calls} tool execution(s).")
    if args.verbose:
        print_trace(messages, model.model_name, perf_counter() - start)
    return 0
