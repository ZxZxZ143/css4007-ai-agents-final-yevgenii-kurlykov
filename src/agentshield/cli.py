import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import get_args
from uuid import uuid4

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

from agentshield.assessment import evaluate_trace_file
from agentshield.config import ConfigurationError, create_model
from agentshield.fixtures import prepare_scenario
from agentshield.graph import GraphResponseError, build_graph
from agentshield.helpdesk import CreationApproval, HelpdeskSession
from agentshield.s1 import run_s1_pair
from agentshield.target import run_target
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


def assessment_command(args: argparse.Namespace) -> int:
    try:
        if args.command == "evaluate":
            result = evaluate_trace_file(args.trace)
            print(result.model_dump_json(indent=2))
            return 0 if result.outcome == "passed" else 1
        model = create_model()
        directory = args.output_dir or Path(".private") / "evaluations" / str(uuid4())
        report = run_s1_pair(model, directory)
    except ConfigurationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error: could not read a valid journal or save the assessment files.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Error: S1 assessment interrupted.", file=sys.stderr)
        return 130
    for run in report.runs:
        print(f"{run.profile} = {run.evaluation.outcome}")
        print(f"  Journal = {run.trace_path}")
        for reason in (*run.evaluation.failures, *run.evaluation.missing_evidence):
            print(f"  {reason}")
    print(f"Assessment saved to {directory / 'evaluation.json'}")
    return 0 if all(run.evaluation.outcome == "passed" for run in report.runs) else 1


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
    target.add_argument("--trace-file", type=Path, help="Export the run journal to this JSON file.")
    evaluate = commands.add_parser("evaluate", help="Independently assess an S1 journal offline.")
    evaluate.add_argument("trace", type=Path)
    paired = commands.add_parser("s1", help="Run and assess S1 in both profiles from fresh fixtures.")
    paired.add_argument("--output-dir", type=Path, help="Save both journals and evaluation.json here.")
    args = parser.parse_args(argv)

    if args.command in ("evaluate", "s1"):
        return assessment_command(args)

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
    session = None
    failure = None
    failure_exit_code = 1
    try:
        if prepared is not None:
            session = HelpdeskSession(prepared, args.profile, approvals)
        model = create_model()
        if session is not None:
            messages = run_target(model, session, question)["messages"]
        else:
            graph = build_graph(model)
            messages = graph.invoke({"messages": [HumanMessage(content=question)]})["messages"]
    except (ConfigurationError, GraphResponseError) as error:
        failure = (str(error), type(error).__name__)
    except APIError as error:
        failure = (api_error_message(error), type(error).__name__)
    except GraphRecursionError:
        failure = ("the graph reached its step limit.", "GraphRecursionError")
    except KeyboardInterrupt:
        failure = ("Execution interrupted.", "KeyboardInterrupt")
        failure_exit_code = 130
    except Exception as error:
        failure = ("Execution failed. Inspect the run journal for the failure type.", type(error).__name__)

    if failure is not None:
        print(f"Error: {failure[0]}", file=sys.stderr)
        if session is not None:
            session.trace.finish("failed", failure[1])
    if session is not None:
        path = args.trace_file or Path(".private") / "traces" / f"{session.run_id}.json"
        try:
            session.trace.write(path)
        except OSError:
            print("Error: could not save the run journal.", file=sys.stderr)
            return 1
        print(f"Trace saved to {path}")
    if failure is not None:
        return failure_exit_code

    print(messages[-1].text)
    model_calls = sum(isinstance(message, AIMessage) for message in messages)
    tool_calls = sum(isinstance(message, ToolMessage) and message.status == "success" for message in messages)
    unsuccessful_calls = sum(isinstance(message, ToolMessage) and message.status == "error" for message in messages)
    print(f"\nCompleted with {model_calls} model response(s) and {tool_calls} tool execution(s).")
    if unsuccessful_calls:
        print(f"{unsuccessful_calls} unsuccessful tool call(s).")
    if args.verbose:
        print_trace(messages, model.model_name, perf_counter() - start)
    return 0
