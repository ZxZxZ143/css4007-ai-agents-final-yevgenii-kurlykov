import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from test_agent_graph import PLAN, finish, touch_tools

from deskpilot import __version__, cli
from deskpilot.cli import console_approver, main
from deskpilot.fake_model import ScriptedChatModel, ai_tool_call
from deskpilot.policy import ApprovalRequest
from deskpilot.policy.approvals import ApprovalChannel

GOAL = "show me what is inside the sandbox"
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.appdata = tmp_path / "appdata"
        monkeypatch.setenv("APPDATA", str(self.appdata))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)  # no real .env is picked up
        self.root = tmp_path / "box"
        self.root.mkdir()
        self.trace = tmp_path / "trace.jsonl"
        self.out = io.StringIO()

    @property
    def cache_dir(self) -> Path:
        return self.appdata / "DeskPilot" / "cache"

    def run(
        self,
        goal: str,
        *extra: str,
        script: list[object] | None = None,
        stdin: str = "",
        tty: bool | None = None,
    ) -> int:
        def make(_model_id: str) -> BaseChatModel:
            return ScriptedChatModel(script=list(script or []))

        argv = ["run", goal, "--mock", "--sandbox", str(self.root), "--trace", str(self.trace)]
        return main(
            [*argv, *extra],
            chat_factory=make,
            stdin=io.StringIO(stdin),
            stdout=self.out,
            tty=tty,
        )

    @property
    def text(self) -> str:
        return self.out.getvalue()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def listing(root: Path) -> list[object]:
    return [PLAN, ai_tool_call("files__list", {"path": str(root)}), finish()]


@win_only
def test_fast_path_needs_no_key_and_no_model(env: Env) -> None:
    def no_model(_model_id: str) -> BaseChatModel:
        raise AssertionError("fast path must not create a model")

    code = main(
        ["run", "открой блокнот", "--mock", "--sandbox", str(env.root), "--trace", str(env.trace)],
        chat_factory=no_model,
        stdin=io.StringIO(),
        stdout=env.out,
    )
    assert code == 0
    assert "stop=fast_path" in env.text
    assert str(env.trace) in env.text
    assert env.trace.is_file()


@win_only
def test_scripted_run_completes_then_cache_hits(env: Env) -> None:
    assert env.run(GOAL, script=listing(env.root)) == 0
    assert "outcome=completed" in env.text and "cache=stored" in env.text
    env.out.truncate(0)
    env.out.seek(0)
    assert env.run(GOAL, script=[]) == 0
    assert "cache=hit" in env.text
    assert (env.cache_dir / "recipes.json").is_file()


@win_only
def test_no_cache_flag(env: Env) -> None:
    assert env.run(GOAL, "--no-cache", script=listing(env.root)) == 0
    assert "cache=disabled" in env.text
    assert not env.cache_dir.exists()


@win_only
def test_json_output_parses(env: Env) -> None:
    assert env.run(GOAL, "--json", script=listing(env.root)) == 0
    data = json.loads(env.text)
    assert data["outcome"] == "completed" and data["trace_path"] == str(env.trace)


@win_only
def test_unfinished_run_exits_one(env: Env) -> None:
    assert env.run(GOAL, script=[PLAN, finish(False, "could not")]) == 1


@win_only
def test_missing_key_with_model_goal_exits_two(env: Env) -> None:
    code = main(
        ["run", GOAL, "--mock", "--sandbox", str(env.root), "--trace", str(env.trace)],
        stdin=io.StringIO(),
        stdout=env.out,
    )
    assert code == 2
    assert "OPENAI_API_KEY" in env.text


@win_only
def test_missing_key_not_needed_for_fast_path(env: Env) -> None:
    code = main(
        ["run", "открой блокнот", "--mock", "--sandbox", str(env.root), "--trace", str(env.trace)],
        stdin=io.StringIO(),
        stdout=env.out,
    )
    assert code == 0


def test_missing_sandbox_exits_two(env: Env) -> None:
    code = main(
        ["run", GOAL, "--mock", "--sandbox", str(env.root / "nope")],
        stdin=io.StringIO(),
        stdout=env.out,
    )
    assert code == 2 and "sandbox" in env.text


def test_missing_settings_file_exits_two(env: Env) -> None:
    code = main(
        ["run", GOAL, "--settings", str(env.root / "nope.toml")],
        stdin=io.StringIO(),
        stdout=env.out,
    )
    assert code == 2


def test_keyboard_interrupt_exits_130(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_goal", boom)
    assert env.run("открой блокнот") == 130
    assert "Остановлено пользователем" in env.text


def approval_request() -> ApprovalRequest:
    now = datetime.now(UTC)
    return ApprovalRequest(
        run_id="r",
        action_fingerprint="a" * 64,
        tool="test.touch",
        summary="touch x",
        channel=ApprovalChannel.TEXT,
        created_at=now,
        expires_at=now + timedelta(seconds=60),
        spoken_prompt="touch x",
        display_text="touch x",
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y", True),
        ("YES", True),
        ("д", True),
        ("да", True),
        ("n", False),
        ("", False),
        ("ok", False),
    ],
)
def test_console_approver_answers(answer: str, expected: bool) -> None:
    out = io.StringIO()
    approve = console_approver(io.StringIO(answer + "\n"), out, "ru", tty=True)
    assert approve(approval_request()) is expected
    assert "Подтвердить? [y/N]" in out.getvalue() and "touch x" in out.getvalue()


def test_console_approver_denies_without_tty() -> None:
    out = io.StringIO()
    approve = console_approver(io.StringIO("y\ny\n"), out, "en", tty=False)
    assert approve(approval_request()) is False
    assert "denied automatically" in out.getvalue()


@win_only
@pytest.mark.parametrize(("tty", "code"), [(True, 0), (False, 1)])
def test_run_with_console_approval(
    env: Env, monkeypatch: pytest.MonkeyPatch, tty: bool, code: int
) -> None:
    executed: list[str] = []
    monkeypatch.setattr(
        cli, "build_deps", partial(cli.build_deps, extra_tools=touch_tools(executed))
    )
    script = [PLAN, ai_tool_call("test__touch", {"name": "x"}), finish()]
    assert env.run("make a marker", "--no-cache", script=script, stdin="y\n", tty=tty) == code
    assert (executed == ["x"]) is tty


def test_version_output(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"deskpilot {__version__}"


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: deskpilot" in capsys.readouterr().out


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code != 0


def test_python_dash_m_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "deskpilot", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"deskpilot {__version__}"


@pytest.mark.parametrize(
    ("goal", "code", "verdict"),
    [
        ("wipe the whole C drive and then format it", 3, "refuse"),
        ("delete the files in the sandbox notes folder", 0, "flag"),
        ("what time is it", 0, "pass"),
    ],
)
def test_check_prints_the_decision_and_exit_code(goal: str, code: int, verdict: str) -> None:
    out = io.StringIO()
    assert main(["check", goal], stdout=out) == code
    assert out.getvalue().startswith(f"verdict={verdict}\n")
    assert "reasons=" in out.getvalue()
