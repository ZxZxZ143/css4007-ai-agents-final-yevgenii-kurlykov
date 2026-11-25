import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from deskpilot.policy.paths import PathPolicy
from deskpilot.policy.types import ReasonCode
from deskpilot.settings import Settings

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

INVALID = ReasonCode.PATH_INVALID
HARD = ReasonCode.PATH_HARD_DENY
PATTERN = ReasonCode.PATH_DENIED_FILE_PATTERN
OUTSIDE = ReasonCode.PATH_OUTSIDE_ALLOWED_ROOTS


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def root(home: Path) -> Path:
    path = home / "docs"
    path.mkdir()
    return path


@pytest.fixture
def env(tmp_path: Path, home: Path) -> dict[str, str]:
    return {
        "WINDIR": str(tmp_path / "Windows"),
        "ProgramFiles": str(tmp_path / "PF"),
        "ProgramFiles(x86)": str(tmp_path / "PF86"),
        "ProgramData": str(tmp_path / "PD"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
    }


def make(root: Path, env: dict[str, str], home: Path, tmp_path: Path, **kw: object) -> PathPolicy:
    return PathPolicy(
        [root],
        settings_dir=tmp_path / "settings",
        install_dir=tmp_path / "install",
        env=env,
        home=home,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def policy(root: Path, env: dict[str, str], home: Path, tmp_path: Path) -> PathPolicy:
    return make(root, env, home, tmp_path)


def test_ok_cases(policy: PathPolicy, root: Path) -> None:
    (root / "dir").mkdir()
    for raw in (
        str(root / "a.txt"),
        str(root / "dir" / "new.txt"),
        str(root / "no" / "such" / "deep" / "f.txt"),
        str(root),
    ):
        result = policy.check(raw)
        assert result.ok and result.reason is None and result.resolved is not None


@pytest.mark.parametrize(
    ("suffix", "reason"),
    [
        ("..\\..\\Windows\\x", HARD),
        ("sub\\..\\..\\outside", OUTSIDE),
        ("..\\docs2\\f", OUTSIDE),
        ("sub\\..\\ok.txt", None),
    ],
)
def test_traversal(policy: PathPolicy, root: Path, suffix: str, reason: ReasonCode | None) -> None:
    result = policy.check(f"{root}\\{suffix}")
    assert result.reason == reason and result.ok == (reason is None)


def test_sibling_prefix(policy: PathPolicy, root: Path) -> None:
    sibling = root.parent / "docs2"
    assert policy.check(str(sibling / "f")).reason == OUTSIDE


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("\\\\server\\share\\x", INVALID),
        ("//server/share/x", INVALID),
        ("\\\\?\\C:\\Windows", INVALID),
        ("\\\\.\\PhysicalDrive0", INVALID),
        ("\\??\\C:\\", INVALID),
        ("C:\\GLOBALROOT\\Device\\x", INVALID),
        ("NUL", INVALID),
        ("relative\\x.txt", INVALID),
        ("C:x.txt", INVALID),
        ("", INVALID),
        ("   ", INVALID),
        ("C:\\a\x00b", INVALID),
        ("%NOPE%\\x", INVALID),
        ("C:\\x" + "a" * 1100, INVALID),
        ("%windir%\\System32", HARD),
        ("%WinDir%\\System32", HARD),
        ("%APPDATA%\\x", HARD),
        ("%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default", HARD),
        ("~\\.ssh\\id_rsa", HARD),
        ("~/.aws/credentials", HARD),
        ("C:\\", HARD),
        ("D:\\", HARD),
    ],
)
def test_attack_table(policy: PathPolicy, raw: str, reason: ReasonCode | None) -> None:
    assert policy.check(raw).reason == reason


@pytest.mark.parametrize("name", ["CON.txt", "nul", "aux.tar.gz", "COM1", "lpt9.x"])
def test_reserved_names(policy: PathPolicy, root: Path, name: str) -> None:
    assert policy.check(f"{root}\\{name}").reason == INVALID


@pytest.mark.parametrize("name", ["file.txt:stream", "name. ", "name.", "a<b", "q?"])
def test_bad_components(policy: PathPolicy, root: Path, name: str) -> None:
    assert policy.check(f"{root}\\{name}").reason == INVALID


@pytest.mark.parametrize("name", ["a\u200bb.txt", "a\u202eb.txt", "\uff41.txt", "x\u2460.txt"])
def test_unicode_tricks(policy: PathPolicy, root: Path, name: str) -> None:
    assert policy.check(f"{root}\\{name}").reason == INVALID


def test_settings_and_install_dirs(
    root: Path, tmp_path: Path, env: dict[str, str], home: Path
) -> None:
    both = PathPolicy(
        [tmp_path],
        settings_dir=tmp_path / "settings",
        install_dir=tmp_path / "install",
        env=env,
        home=home,
    )
    assert both.check(str(tmp_path / "settings" / "s.toml")).reason == HARD
    assert both.check(str(tmp_path / "install" / "m.py")).reason == HARD
    assert both.check(str(tmp_path / "other.txt")).ok


def test_root_inside_hard_deny_rejected(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    inside = home / "AppData" / "Roaming" / "foo"
    inside.mkdir(parents=True)
    with pytest.raises(ValueError):
        make(inside, env, home, tmp_path)
    with pytest.raises(ValueError):
        make(tmp_path / "w", env, home, tmp_path, sandbox_dir=Path(env["WINDIR"]) / "sb")


def test_drive_root_rejected(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    for drive in (Path(tmp_path.anchor), Path("C:\\")):
        with pytest.raises(ValueError):
            make(drive, env, home, tmp_path)
        with pytest.raises(ValueError):
            make(tmp_path, env, home, tmp_path, sandbox_dir=drive)


def test_root_containing_hard_deny_allowed_but_denied(
    env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    pol = make(home, env, home, tmp_path)
    assert pol.check(str(home / "AppData" / "Roaming" / "x")).reason == HARD
    assert pol.check(str(home / ".ssh" / "id_rsa")).reason == HARD
    assert pol.check(str(home / "ok.txt")).ok


def test_empty_env_keeps_system_ceilings(root: Path, home: Path, tmp_path: Path) -> None:
    pol = PathPolicy([root], env={}, home=home, settings_dir=tmp_path / "s")
    real_home = Path.home()
    for raw in (
        "C:\\Windows\\win.ini",
        "C:\\Program Files\\x",
        "C:\\ProgramData\\x",
        str(real_home / "AppData" / "Roaming" / "x"),
        str(real_home / ".ssh" / "id_rsa"),
    ):
        assert pol.check(raw).reason == HARD, raw


def test_fake_home_keeps_real_home_denies(
    root: Path, env: dict[str, str], home: Path, tmp_path: Path
) -> None:
    pol = make(root, env, home, tmp_path)
    assert pol.check(str(Path.home() / ".ssh" / "id_rsa")).reason == HARD
    assert pol.check(str(Path.home() / ".aws" / "credentials")).reason == HARD


@pytest.mark.parametrize(
    "rel", [".env", "x.PEM", "a.key", "server.KEY", ".env\\a.txt", "sub\\.ENV"]
)
def test_file_patterns(policy: PathPolicy, root: Path, rel: str) -> None:
    assert policy.check(f"{root}\\{rel}").reason == PATTERN


def test_case_tricks(root: Path, env: dict[str, str], home: Path, tmp_path: Path) -> None:
    pol = make(Path(str(root).upper()), env, home, tmp_path)
    assert pol.check(str(root / "F.txt").lower()).ok
    assert pol.check(str(tmp_path / "WINDOWS" / "x").lower()).reason == HARD
    pol2 = PathPolicy([root], env={"WinDir": env["WINDIR"]}, home=home, settings_dir=tmp_path / "s")
    assert pol2.check("%WINDIR%\\x").reason == HARD
    assert pol2.check("c:\\WINDOWS\\System32").reason == HARD


def _junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stderr.strip()}")


def test_junction_to_windows(root: Path, home: Path, tmp_path: Path) -> None:
    windir = os.environ.get("WINDIR")
    if not windir or not Path(windir).is_dir():
        pytest.skip("no WINDIR")
    pol = PathPolicy([root], env={"WINDIR": windir}, home=home, settings_dir=tmp_path / "s")
    link = root / "w"
    _junction(link, Path(windir))
    try:
        assert pol.check(str(link / "System32" / "x")).reason == HARD
        assert pol.check(str(link)).reason == HARD
    finally:
        os.rmdir(link)


def test_junction_outside(policy: PathPolicy, root: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = root / "out"
    _junction(link, target)
    try:
        assert policy.check(str(link / "f.txt")).reason == OUTSIDE
    finally:
        os.rmdir(link)


def test_symlink_outside(policy: PathPolicy, root: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere2"
    target.mkdir()
    link = root / "sym"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted")
    try:
        assert policy.check(str(link / "f.txt")).reason == OUTSIDE
    finally:
        os.rmdir(link)


def _short(path: Path) -> str:
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))  # type: ignore[attr-defined]
    return buf.value


def test_short_name_alias(home: Path, tmp_path: Path) -> None:
    long_dir = tmp_path / "a_very_long_directory_name"
    long_dir.mkdir()
    short = _short(long_dir)
    if not short or short.lower() == str(long_dir).lower():
        pytest.skip("8.3 names disabled")
    env = {"WINDIR": str(long_dir)}
    denied = PathPolicy([tmp_path], env=env, home=home, settings_dir=tmp_path / "s")
    by_long = denied.check(str(long_dir / "f.txt"))
    by_short = denied.check(f"{short}\\f.txt")
    assert by_short.reason == by_long.reason == HARD
    plain = PathPolicy([long_dir], env={"WINDIR": str(tmp_path / "w")}, home=home)
    ok_long = plain.check(str(long_dir / "f.txt"))
    ok_short = plain.check(f"{short}\\f.txt")
    assert ok_long.ok and ok_short.ok and ok_long.resolved == ok_short.resolved


def test_sandbox_counts_as_root(env: dict[str, str], home: Path, tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    pol = PathPolicy([], sandbox, env=env, home=home, settings_dir=tmp_path / "s")
    assert pol.check(str(sandbox / "f.txt")).ok


def test_check_all(policy: PathPolicy, root: Path) -> None:
    assert policy.check_all([]).ok
    assert policy.check_all([]).resolved is None
    good, bad = str(root / "a"), str(root / ".env")
    assert policy.check_all([good, bad, "C:\\"]).reason == PATTERN
    assert policy.check_all([good, str(root / "b")]).ok


def test_from_settings(root: Path, env: dict[str, str], home: Path, tmp_path: Path) -> None:
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    settings = Settings(allowed_roots=(root,), sandbox_dir=sandbox)
    pol = PathPolicy.from_settings(settings, env=env, home=home, settings_dir=tmp_path / "s")
    assert pol.check(str(root / "a")).ok and pol.check(str(sandbox / "a")).ok
    mapped = Settings.from_mapping({"allowed_roots": [str(root)]})
    pol2 = PathPolicy.from_settings(mapped, env=env, home=home, settings_dir=tmp_path / "s")
    assert pol2.check(str(root / "a")).ok
    assert pol2.check(str(sandbox / "a")).reason == OUTSIDE
