import os
import sys
from pathlib import Path

import pytest

from deskpilot.adapters import (
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolRegistry,
    register_file_tools,
)
from deskpilot.adapters.files import MAX_COPY_FILES, recycle_bin_from
from deskpilot.policy import (
    ActionGate,
    ApprovalStore,
    BudgetTracker,
    PathPolicy,
    PolicyDenied,
    ReasonCode,
    RiskClass,
    Verdict,
)
from deskpilot.settings import Settings

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


class Env:
    def __init__(self, tmp: Path, sandbox: bool = True) -> None:
        self.tmp = tmp
        self.root = tmp / "sandbox"
        self.root.mkdir()
        self.settings = Settings(allowed_roots=(self.root,))
        policy = PathPolicy([self.root], settings_dir=tmp / "settings")
        self.registry = ToolRegistry()
        register_file_tools(self.registry)
        self.approvals = ApprovalStore()
        self.gate = ActionGate(
            self.settings,
            tool_risks=self.registry.tool_risks(),
            paths=policy,
            budgets=BudgetTracker(self.settings.budgets),
            approvals=self.approvals,
            file_ops=self.registry.file_ops,
        )
        self.ctx = ToolContext(
            run_id="run1",
            settings=self.settings,
            path_policy=policy,
            mode="mock",
            sandbox_dir=self.root if sandbox else None,
        )

    def run(self, tool: str, **args: object) -> ToolOutcome:
        action = self.registry.build_action("run1", tool, args)
        decision = self.gate.evaluate(action)
        approval_id = None
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            request = self.gate.request_approval(action, decision)
            self.approvals.resolve_text(request.approval_id, True)
            approval_id = request.approval_id
        permit = self.gate.authorize(action, approval_id=approval_id)
        return self.registry.dispatch(action, permit, self.ctx)

    def decision(self, tool: str, **args: object) -> Verdict:
        return self.gate.evaluate(self.registry.build_action("run1", tool, args)).verdict


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def kind(outcome: ToolOutcome) -> str | None:
    return outcome.error.kind if outcome.error else None


def verified(outcome: ToolOutcome) -> bool:
    return outcome.ok and outcome.verify is not None and outcome.verify.ok


# -- registry, risks, gate -----------------------------------------------------


def test_all_tools_register_and_are_model_visible() -> None:
    registry = ToolRegistry()
    register_file_tools(registry)
    assert set(registry.names()) == {
        "files.search",
        "files.create_folder",
        "files.create_file",
        "files.write",
        "files.rename",
        "files.move",
        "files.copy",
        "files.delete",
    }
    assert len(registry.model_tools()) == 8


def test_risk_classes_and_declared_paths(env: Env) -> None:
    def build(tool: str, **a: object):  # noqa: ANN202
        return env.registry.build_action("run1", tool, a)

    r = str(env.root)
    assert build("files.search", root=r, pattern="*").risk_class is RiskClass.READ
    assert build("files.create_folder", path=r + r"\d").risk_class is RiskClass.MODIFY
    assert build("files.write", path=r + r"\a.txt", text="x").risk_class is RiskClass.MODIFY
    over = build("files.write", path=r + r"\a.txt", text="x", overwrite=True)
    assert over.risk_class is RiskClass.DANGEROUS
    assert build("files.delete", path=r + r"\a.txt").risk_class is RiskClass.DANGEROUS
    assert build("files.rename", path=r + r"\a.txt", new_name="b.txt").paths == (
        r + r"\a.txt",
        r + r"\b.txt",
    )
    for tool in ("files.move", "files.copy"):
        action = build(tool, src=r + r"\a.txt", dst_dir=r + r"\d")
        assert action.paths == (r + r"\a.txt", r + r"\d", r + r"\d\a.txt")


def test_modify_and_dangerous_need_approval(env: Env) -> None:
    r = str(env.root)
    assert env.decision("files.search", root=r, pattern="*") is Verdict.ALLOW
    assert env.decision("files.create_file", path=r + r"\a.txt") is Verdict.NEEDS_APPROVAL
    assert env.decision("files.delete", path=r + r"\a.txt") is Verdict.NEEDS_APPROVAL
    assert env.decision("files.write", path=r + r"\a.txt", overwrite=True) is Verdict.NEEDS_APPROVAL


def test_unapproved_action_is_not_executed(env: Env) -> None:
    action = env.registry.build_action(
        "run1", "files.create_file", {"path": str(env.root / "a.txt")}
    )
    with pytest.raises(PolicyDenied):
        env.gate.authorize(action)
    assert not (env.root / "a.txt").exists()


def test_destination_in_hard_deny_zone_or_outside_roots_is_denied(env: Env) -> None:
    src = env.root / "a.txt"
    src.write_text("x")
    cases = [
        ("files.copy", {"src": str(src), "dst_dir": r"C:\Windows"}, ReasonCode.PATH_HARD_DENY),
        ("files.move", {"src": str(src), "dst_dir": str(env.tmp)}, None),
        (
            "files.rename",
            {"path": str(src), "new_name": ".env"},
            ReasonCode.PATH_DENIED_FILE_PATTERN,
        ),
        (
            "files.rename",
            {"path": str(src), "new_name": "id.key"},
            ReasonCode.PATH_DENIED_FILE_PATTERN,
        ),
    ]
    for tool, args, reason in cases:
        action = env.registry.build_action("run1", tool, args)
        with pytest.raises(PolicyDenied) as exc:
            env.gate.authorize(action)
        if reason is not None:
            assert exc.value.decision.reason is reason
    assert src.exists()


@pytest.mark.parametrize("name", ["..\\x", "a/b", "C:x", "a:b", "..", "x ", "a|b"])
def test_rename_rejects_non_bare_names(env: Env, name: str) -> None:
    with pytest.raises(ToolError) as exc:
        env.registry.build_action(
            "run1", "files.rename", {"path": str(env.root / "a"), "new_name": name}
        )
    assert exc.value.kind == "invalid_args"


def test_write_text_length_is_capped(env: Env) -> None:
    with pytest.raises(ToolError):
        env.registry.build_action(
            "run1", "files.write", {"path": str(env.root / "a.txt"), "text": "x" * 100_001}
        )


# -- sandbox behaviour ---------------------------------------------------------


def test_search_matches_untrusted_and_skips_denied(env: Env) -> None:
    (env.root / "sub" / "deep").mkdir(parents=True)
    (env.root / "Report.TXT").write_text("1")
    (env.root / "sub" / "deep" / "r2.txt").write_text("2")
    (env.root / "sub" / "secret.key").write_text("k")
    (env.root / "sub" / "other.md").write_text("3")
    out = env.run("files.search", root=str(env.root), pattern="*.txt")
    assert out.ok and out.untrusted and verified(out)
    assert sorted(Path(m).name for m in out.data["matches"]) == ["Report.TXT", "r2.txt"]
    keys = env.run("files.search", root=str(env.root), pattern="*.key")
    assert keys.data["matches"] == []
    shallow = env.run("files.search", root=str(env.root), pattern="*.txt", max_depth=0)
    assert [Path(m).name for m in shallow.data["matches"]] == ["Report.TXT"]


def test_search_truncates_at_100_matches(env: Env) -> None:
    for i in range(130):
        (env.root / f"f{i:03}.txt").write_text("")
    out = env.run("files.search", root=str(env.root), pattern="*.txt")
    assert len(out.data["matches"]) == 100
    assert out.data["truncated"] is True


def test_create_folder_and_no_overwrite(env: Env) -> None:
    out = env.run("files.create_folder", path=str(env.root / "new"))
    assert verified(out)
    assert kind(env.run("files.create_folder", path=str(env.root / "new"))) == "invalid_args"
    assert kind(env.run("files.create_folder", path=str(env.root / "x" / "y"))) == "not_found"


def test_create_file_and_no_overwrite(env: Env) -> None:
    out = env.run("files.create_file", path=str(env.root / "a.txt"), text="héllo")
    assert verified(out)
    assert (env.root / "a.txt").read_bytes() == "héllo".encode()
    assert kind(env.run("files.create_file", path=str(env.root / "a.txt"))) == "invalid_args"
    assert (env.root / "a.txt").read_text(encoding="utf-8") == "héllo"


def test_write_new_then_refuse_then_overwrite(env: Env) -> None:
    path = str(env.root / "n.md")
    assert verified(env.run("files.write", path=path, text="one"))
    assert kind(env.run("files.write", path=path, text="two")) == "invalid_args"
    assert (env.root / "n.md").read_text() == "one"
    assert verified(env.run("files.write", path=path, text="three", overwrite=True))
    assert (env.root / "n.md").read_text() == "three"


@pytest.mark.parametrize("name", ["a.exe", "a.BAT", "a.ps1", "a.lnk", "a.js", "a.reg", "a.dll"])
def test_executable_suffixes_are_refused(env: Env, name: str) -> None:
    path = str(env.root / name)
    assert kind(env.run("files.create_file", path=path)) == "unsupported"
    assert kind(env.run("files.write", path=path, text="x")) == "unsupported"
    assert not (env.root / name).exists()


def test_write_allows_only_text_suffixes(env: Env) -> None:
    assert kind(env.run("files.write", path=str(env.root / "a.bin"), text="x")) == "unsupported"
    assert kind(env.run("files.write", path=str(env.root / "noext"), text="x")) == "unsupported"


def test_rename_move_and_copy_refuse_executable_destinations(env: Env) -> None:
    (env.root / "a.txt").write_text("x")
    (env.root / "d").mkdir()
    assert (
        kind(env.run("files.rename", path=str(env.root / "a.txt"), new_name="a.exe"))
        == "unsupported"
    )
    (env.root / "run.exe").write_text("x")
    assert (
        kind(env.run("files.copy", src=str(env.root / "run.exe"), dst_dir=str(env.root / "d")))
        == "unsupported"
    )
    assert (
        kind(env.run("files.move", src=str(env.root / "run.exe"), dst_dir=str(env.root / "d")))
        == "unsupported"
    )
    assert (env.root / "a.txt").exists() and not (env.root / "d" / "run.exe").exists()


def test_rename(env: Env) -> None:
    (env.root / "a.txt").write_text("x")
    (env.root / "b.txt").write_text("y")
    assert verified(env.run("files.rename", path=str(env.root / "a.txt"), new_name="c.txt"))
    assert (env.root / "c.txt").read_text() == "x" and not (env.root / "a.txt").exists()
    assert (
        kind(env.run("files.rename", path=str(env.root / "c.txt"), new_name="b.txt"))
        == "invalid_args"
    )
    assert (
        kind(env.run("files.rename", path=str(env.root / "none.txt"), new_name="z.txt"))
        == "not_found"
    )


def test_move_never_overwrites(env: Env) -> None:
    (env.root / "d").mkdir()
    (env.root / "a.txt").write_text("x")
    assert verified(env.run("files.move", src=str(env.root / "a.txt"), dst_dir=str(env.root / "d")))
    assert (env.root / "d" / "a.txt").read_text() == "x"
    (env.root / "a.txt").write_text("new")
    out = env.run("files.move", src=str(env.root / "a.txt"), dst_dir=str(env.root / "d"))
    assert kind(out) == "invalid_args"
    assert (env.root / "d" / "a.txt").read_text() == "x" and (env.root / "a.txt").exists()


def test_move_folder_into_itself_is_refused(env: Env) -> None:
    (env.root / "d" / "e").mkdir(parents=True)
    out = env.run("files.move", src=str(env.root / "d"), dst_dir=str(env.root / "d" / "e"))
    assert kind(out) == "invalid_args"


def test_move_into_root_is_allowed(env: Env) -> None:
    (env.root / "d").mkdir()
    (env.root / "d" / "a.txt").write_text("x")
    assert verified(env.run("files.move", src=str(env.root / "d" / "a.txt"), dst_dir=str(env.root)))


def test_copy_file_and_folder(env: Env) -> None:
    (env.root / "d").mkdir()
    (env.root / "a.txt").write_text("abc")
    out = env.run("files.copy", src=str(env.root / "a.txt"), dst_dir=str(env.root / "d"))
    assert verified(out) and (env.root / "a.txt").exists()
    assert (
        kind(env.run("files.copy", src=str(env.root / "a.txt"), dst_dir=str(env.root / "d")))
        == "invalid_args"
    )
    (env.root / "tree" / "inner").mkdir(parents=True)
    (env.root / "tree" / "inner" / "f.txt").write_text("deep")
    out = env.run("files.copy", src=str(env.root / "tree"), dst_dir=str(env.root / "d"))
    assert verified(out) and out.data["files"] == 1
    assert (env.root / "d" / "tree" / "inner" / "f.txt").read_text() == "deep"


def test_copy_over_file_cap_copies_nothing(env: Env) -> None:
    (env.root / "big").mkdir()
    (env.root / "d").mkdir()
    for i in range(MAX_COPY_FILES + 1):
        (env.root / "big" / f"f{i}.txt").write_text("")
    out = env.run("files.copy", src=str(env.root / "big"), dst_dir=str(env.root / "d"))
    assert kind(out) == "invalid_args"
    assert not (env.root / "d" / "big").exists()


def test_mock_delete_goes_to_trash(env: Env) -> None:
    (env.root / "a.txt").write_text("x")
    out = env.run("files.delete", path=str(env.root / "a.txt"))
    assert verified(out)
    assert not (env.root / "a.txt").exists()
    trashed = Path(out.data["recycled_to"])
    assert trashed.parent == env.root / ".deskpilot-trash" and trashed.read_text() == "x"
    assert kind(env.run("files.delete", path=str(env.root / "a.txt"))) == "not_found"


def test_root_itself_cannot_be_changed(env: Env) -> None:
    assert kind(env.run("files.delete", path=str(env.root))) == "permission"
    with pytest.raises(PolicyDenied):  # the renamed root would sit outside every root
        env.run("files.rename", path=str(env.root), new_name="other")
    assert env.root.exists()


def test_mock_without_sandbox_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path, sandbox=False)
    assert kind(env.run("files.create_file", path=str(env.root / "a.txt"))) == "unsupported"
    assert not (env.root / "a.txt").exists()
    assert env.run("files.search", root=str(env.root), pattern="*").ok


def test_symlinked_destination_outside_sandbox_is_denied(env: Env) -> None:
    outside = env.tmp / "outside"
    outside.mkdir()
    (env.root / "a.txt").write_text("x")
    try:
        os.symlink(outside, env.root / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not permitted here")
    action = env.registry.build_action(
        "run1", "files.copy", {"src": str(env.root / "a.txt"), "dst_dir": str(env.root / "link")}
    )
    with pytest.raises(PolicyDenied) as exc:
        env.gate.authorize(action)
    assert exc.value.decision.reason is ReasonCode.PATH_OUTSIDE_ALLOWED_ROOTS
    assert not (outside / "a.txt").exists()


def test_search_does_not_follow_links(env: Env) -> None:
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "hidden.txt").write_text("x")
    try:
        os.symlink(outside, env.root / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not permitted here")
    out = env.run("files.search", root=str(env.root), pattern="*.txt")
    assert out.data["matches"] == []


def test_recycle_bin_argument_is_double_nul_terminated() -> None:
    text = recycle_bin_from(Path(r"C:\x\a.txt"))
    assert text == "C:\\x\\a.txt\0\0"
