"""Matched S1 runs with independent journal-based assessment."""

from pathlib import Path
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

from agentshield.assessment import evaluate_trace_file
from agentshield.evaluation import EvaluationResult
from agentshield.fixtures import FixtureRecord, S1, prepare_scenario
from agentshield.helpdesk import HelpdeskSession
from agentshield.target import run_target
from agentshield.trace import sanitize, write_json_atomic


class S1RunAssessment(FixtureRecord):
    profile: Literal["baseline", "protected"]
    trace_path: str
    evaluation: EvaluationResult


class S1PairAssessment(FixtureRecord):
    scenario_id: Literal["S1"] = "S1"
    scenario_revision: str
    fixture_revision: str
    model_adapter: str
    model: str | None
    runs: tuple[S1RunAssessment, ...]


def run_s1_pair(model: BaseChatModel, directory: Path) -> S1PairAssessment:
    runs = []
    for profile in ("baseline", "protected"):
        prepared = prepare_scenario("S1")
        session = HelpdeskSession(prepared, profile)
        try:
            run_target(model, session, prepared.scenario.goal)
        except Exception:
            # The runner has recorded the failure and final snapshot for evaluation.
            pass
        except KeyboardInterrupt:
            session.trace.write(directory / f"{prepared.run_id}.json")
            raise
        trace_path = directory / f"{prepared.run_id}.json"
        session.trace.write(trace_path)
        evaluation = evaluate_trace_file(trace_path)
        runs.append(S1RunAssessment(profile=profile, trace_path=str(trace_path.resolve()), evaluation=evaluation))
    name = getattr(model, "model_name", None)
    report = S1PairAssessment(
        scenario_revision=S1.revision, fixture_revision=S1.fixture_revision,
        model_adapter=type(model).__name__, model=sanitize(name) if isinstance(name, str) else None, runs=tuple(runs),
    )
    write_json_atomic(directory / "evaluation.json", report.model_dump_json(indent=2))
    return report
