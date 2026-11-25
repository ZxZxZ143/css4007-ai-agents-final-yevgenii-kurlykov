"""Command line entry point: `deskpilot run "<goal>"`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal, TextIO

from langchain_core.language_models.chat_models import BaseChatModel

from deskpilot import __version__
from deskpilot.agent import RunResult, build_deps, run_goal
from deskpilot.agent.runner import report_for
from deskpilot.cache import RecipeCache
from deskpilot.model import ModelConfig, ModelConfigError, create_openai_chat, load_api_key
from deskpilot.policy import ApprovalRequest
from deskpilot.settings import Settings, SettingsError, default_settings_path, load_settings
from deskpilot.trace import Outcome

ChatFactory = Callable[[str], BaseChatModel]

EXIT_OK = 0
EXIT_NOT_COMPLETED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

_YES = frozenset({"y", "yes", "д", "да"})
_TEXT = {
    "ru": {
        "approve": "Подтвердить? [y/N] ",
        "action": "Требуется подтверждение действия",
        "non_tty": "Ввод не является терминалом: действие отклонено автоматически.",
        "no_key": "Ошибка: не задан OPENAI_API_KEY. Добавьте его в файл .env или в окружение.",
        "interrupted": "Остановлено пользователем",
        "trace": "Трассировка",
    },
    "en": {
        "approve": "Approve? [y/N] ",
        "action": "Action needs approval",
        "non_tty": "Input is not a terminal: the action was denied automatically.",
        "no_key": "Error: OPENAI_API_KEY is not set. Put it in your .env file or the environment.",
        "interrupted": "Stopped by the user",
        "trace": "Trace",
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deskpilot", description="Policy-gated desktop agent.")
    parser.add_argument("--version", action="version", version=f"deskpilot {__version__}")
    commands = parser.add_subparsers(dest="command", title="commands")
    run = commands.add_parser("run", help="run one goal")
    run.add_argument("goal", help="what the agent should do")
    run.add_argument("--mock", action="store_true", help="use mock adapters (no real desktop)")
    run.add_argument("--sandbox", type=Path, help="existing directory used as the only file zone")
    run.add_argument("--no-cache", action="store_true", help="do not use the recipe cache")
    run.add_argument("--settings", type=Path, help="settings TOML file")
    run.add_argument("--trace", type=Path, help="trace JSONL path")
    run.add_argument("--json", action="store_true", help="print the run result as JSON")
    run.add_argument("--model", help="fast model id")
    run.add_argument("--strong-model", help="strong model id")
    return parser


def _with(settings: Settings, **updates: Any) -> Settings:
    return Settings.model_validate({**settings.model_dump(), **updates})


def _prepare_settings(args: argparse.Namespace) -> Settings:
    if args.settings is not None and not args.settings.is_file():
        raise SettingsError(f"settings file not found: {args.settings}")
    settings = load_settings(args.settings).settings
    updates: dict[str, Any] = {}
    if args.sandbox is not None:
        if not args.sandbox.is_dir():
            raise SettingsError(f"sandbox directory does not exist: {args.sandbox}")
        root = args.sandbox.resolve()
        updates.update(allowed_roots=(root,), sandbox_dir=root)
    if args.model:
        updates["model_id"] = args.model
    if args.strong_model:
        updates["strong_model_id"] = args.strong_model
    return _with(settings, **updates) if updates else settings


def console_approver(
    stdin: TextIO, stdout: TextIO, language: Literal["ru", "en"], *, tty: bool
) -> Callable[[ApprovalRequest], bool]:
    """Ask on the console. Only an explicit yes approves; a non-terminal input always denies."""
    text = _TEXT[language]

    def approve(request: ApprovalRequest) -> bool:
        print(f"{text['action']}: {request.tool}\n{request.summary}", file=stdout)
        if not tty:
            print(text["non_tty"], file=stdout)
            return False
        print(text["approve"], end="", file=stdout, flush=True)
        return stdin.readline().strip().lower() in _YES

    return approve


def _chat_factory(missing_key: list[bool]) -> ChatFactory:
    def make(model_id: str) -> BaseChatModel:
        try:
            key = load_api_key(Path(".env"))
        except ModelConfigError:
            missing_key.append(True)
            raise
        return create_openai_chat(ModelConfig(model_id=model_id), key)

    return make


def _report(result: RunResult, language: Literal["ru", "en"]) -> str:
    report = getattr(result, "report", None)
    if isinstance(report, str) and report:
        return report
    return report_for(result, language)


def _summary_line(result: RunResult) -> str:
    return (
        f"outcome={result.outcome.value} stop={result.stop_reason} steps={len(result.steps)} "
        f"model_calls={result.model_calls} tokens={result.tokens} cache={result.cache} "
        f"tier={result.tier.start}->{result.tier.final}"
    )


def _run(
    args: argparse.Namespace,
    chat_factory: ChatFactory | None,
    stdin: TextIO,
    stdout: TextIO,
    tty: bool,
) -> int:
    settings = _prepare_settings(args)
    base = default_settings_path().parent
    cache: RecipeCache | None = None
    if not args.no_cache and settings.cache.enabled:
        cache = RecipeCache(
            base / "cache" / "recipes.json",
            ttl_s=settings.cache.ttl_days * 86400.0,
            max_entries=settings.cache.max_entries,
        )
    trace_path = args.trace or base / "traces" / f"{date.today().isoformat()}.jsonl"
    missing_key: list[bool] = []
    deps = build_deps(
        settings,
        args.goal,
        chat_factory=chat_factory or _chat_factory(missing_key),
        mode="mock" if args.mock else "real",
        trace_path=trace_path,
        cache=cache,
        approver=console_approver(stdin, stdout, settings.language, tty=tty),
    )
    result = run_goal(args.goal, deps)
    if missing_key:
        print(_TEXT[settings.language]["no_key"], file=stdout)
        return EXIT_USAGE
    if args.json:
        print(result.model_dump_json(indent=2), file=stdout)
    else:
        print(_report(result, settings.language), file=stdout)
        print(_summary_line(result), file=stdout)
        print(f"{_TEXT[settings.language]['trace']}: {result.trace_path}", file=stdout)
    return EXIT_OK if result.outcome is Outcome.COMPLETED else EXIT_NOT_COMPLETED


def main(
    argv: list[str] | None = None,
    *,
    chat_factory: ChatFactory | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    tty: bool | None = None,
) -> int:
    """`tty` is a test hook; by default the terminal state of `stdin` decides."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    is_tty = stdin.isatty() if tty is None else tty
    try:
        return _run(args, chat_factory, stdin, stdout, is_tty)
    except SettingsError as exc:
        print(f"Ошибка настроек: {exc}", file=stdout)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print(_TEXT["ru"]["interrupted"], file=stdout)
        return EXIT_INTERRUPTED
