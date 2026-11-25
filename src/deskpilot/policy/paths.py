"""Path policy: expansion, syntax rejection, real-path resolution, zone checks.

Hard deny always wins over allowed roots. Paths are judged only after real-path
resolution (symlinks, junctions, `..`, 8.3 short names).

Hard-deny dirs are the union of the caller's env/home expansion and an OS-derived
set (Windows dir, known folders, real profile) that ignores `env`, so a minimal
or hostile env cannot remove a ceiling. A template unresolvable from both raises.

Roots: a drive root or a root inside a hard-deny zone raises ValueError at
construction; a root that merely contains hard-deny dirs is allowed (hard deny
still wins at check time).

check() is check-then-use: executors must re-check right before the filesystem
call, because a junction or symlink may be created after the check (TOCTOU).
"""

import ctypes
import fnmatch
import os
import re
import sys
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict

import deskpilot
from deskpilot.limits import (
    HARD_DENY_DIR_TEMPLATES,
    HARD_DENY_DRIVE_ROOTS,
    HARD_DENY_FILE_PATTERNS,
    HARD_DENY_INSTALL_DIR,
    HARD_DENY_SETTINGS_DIR,
)
from deskpilot.policy.types import ReasonCode
from deskpilot.settings import Settings, default_settings_path

MAX_PATH_CHARS = 1024
_VAR_RE = re.compile(r"%([^%\\/]+)%")
_DRIVE_RE = re.compile(r"^[A-Za-z]:\\")
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL"}) | frozenset(
    f"{name}{i}" for name in ("COM", "LPT") for i in range(1, 10)
)
_BAD_CHARS = frozenset('<>"|?*')


class PathCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: ReasonCode | None
    raw: str
    resolved: Path | None
    detail: str


class _Invalid(Exception):
    pass


def _expand(raw: str, env: Mapping[str, str], home: Path) -> str:
    folded = {k.casefold(): v for k, v in env.items()}

    def repl(match: re.Match[str]) -> str:
        value = folded.get(match.group(1).casefold())
        if value is None:
            raise _Invalid("unknown environment variable")
        return value

    out = _VAR_RE.sub(repl, raw)
    if "%" in out:
        raise _Invalid("stray % in path")
    if out == "~" or out[:2] in ("~/", "~\\"):
        out = str(home) + out[1:]
    return out


def _validate_syntax(expanded: str) -> None:
    text = expanded.replace("/", "\\")
    if text.startswith("\\\\") or text.startswith("\\??\\"):
        raise _Invalid("UNC or device path")
    if "globalroot" in text.casefold():
        raise _Invalid("device namespace path")
    if not _DRIVE_RE.match(text):
        raise _Invalid("path must be absolute with a drive letter")
    rest = text[3:]
    if ":" in rest:
        raise _Invalid("alternate data stream")
    if any(c in _BAD_CHARS for c in rest):
        raise _Invalid("illegal character")
    for comp in rest.split("\\"):
        if not comp or comp in (".", ".."):
            continue
        if comp[-1] in ". ":
            raise _Invalid("component ends with dot or space")
        if comp.split(".")[0].upper() in _RESERVED:
            raise _Invalid("reserved device name")


def _long_name(path: str) -> str:
    """Expand 8.3 aliases on the deepest existing ancestor; no-op off Windows."""
    if sys.platform != "win32":
        return path
    head, tail = path, []
    while not os.path.exists(head):
        parent, name = os.path.split(head)
        if parent == head:
            return path
        head, tail = parent, [name, *tail]
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetLongPathNameW(head, buf, len(buf))  # type: ignore[attr-defined]
    if n == 0 or n >= len(buf):
        return path
    return os.path.join(buf.value, *tail)


def _real(path: str) -> Path:
    return Path(_long_name(os.path.realpath(path, strict=False)))


def _is_drive_root(path: str) -> bool:
    p = PureWindowsPath(path)
    return p.parent == p


def _parts(path: Path) -> tuple[str, ...]:
    return tuple(os.path.normcase(p) for p in path.parts)


def _inside(path: Path, root: Path) -> bool:
    p, r = _parts(path), _parts(root)
    return len(p) >= len(r) and p[: len(r)] == r


def _known_folder(guid: str) -> str | None:
    if sys.platform != "win32":
        return None
    buf = (ctypes.c_char * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
    ptr = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    if shell32.SHGetKnownFolderPath(ctypes.byref(buf), 0, None, ctypes.byref(ptr)) != 0:
        return None
    value = ptr.value
    ctypes.windll.ole32.CoTaskMemFree(ptr)  # type: ignore[attr-defined]
    return value


def _windows_dir() -> str | None:
    if sys.platform != "win32":
        return None
    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetWindowsDirectoryW(buf, len(buf))  # type: ignore[attr-defined]
    return buf.value if 0 < n < len(buf) else None


def _os_env(real_home: Path) -> dict[str, str]:
    """System locations independent of the caller's env; literals as last resort."""
    spec = {
        "ProgramFiles": ("905e63b6-c1bf-494e-b29c-65b732d3d21a", r"C:\Program Files"),
        "ProgramFiles(x86)": ("7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E", r"C:\Program Files (x86)"),
        "ProgramData": ("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97", r"C:\ProgramData"),
        "APPDATA": (
            "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",
            str(real_home / "AppData" / "Roaming"),
        ),
        "LOCALAPPDATA": (
            "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
            str(real_home / "AppData" / "Local"),
        ),
    }
    out = {"WINDIR": _windows_dir() or os.environ.get("WINDIR") or r"C:\Windows"}
    for name, (guid, literal) in spec.items():
        out[name] = _known_folder(guid) or os.environ.get(name) or literal
    return out


def _try_expand(template: str, env: Mapping[str, str], home: Path) -> Path | None:
    try:
        expanded = _expand(template, env, home)
        _validate_syntax(expanded)
    except _Invalid:
        return None
    return _real(expanded)


class PathPolicy:
    def __init__(
        self,
        allowed_roots: Iterable[Path],
        sandbox_dir: Path | None = None,
        *,
        settings_dir: Path | None = None,
        install_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        real_home = Path.home()
        contexts = [
            (os.environ if env is None else env, real_home if home is None else home),
            (_os_env(real_home), real_home),
        ]
        self._hard_dirs: list[Path] = []
        for template in HARD_DENY_DIR_TEMPLATES:
            found = [d for e, h in contexts if (d := _try_expand(template, e, h)) is not None]
            if not found:
                raise ValueError(f"hard-deny template cannot be resolved: {template}")
            self._hard_dirs.extend(found)
        if HARD_DENY_SETTINGS_DIR:
            self._hard_dirs.append(_real(str(settings_dir or default_settings_path().parent)))
        if HARD_DENY_INSTALL_DIR:
            self._hard_dirs.append(_real(str(install_dir or Path(deskpilot.__file__).parent)))
        self._env, self._home = contexts[0]
        self._roots = [
            self._safe_root(r) for r in [*allowed_roots, *([sandbox_dir] if sandbox_dir else [])]
        ]

    def _safe_root(self, root: Path) -> Path:
        real = _real(str(root))
        if _is_drive_root(str(real)):
            raise ValueError(f"drive roots are not allowed: {root}")
        if any(_inside(real, d) for d in self._hard_dirs):
            raise ValueError(f"root is inside a protected zone: {root}")
        return real

    @classmethod
    def from_settings(cls, settings: Settings, **kw: object) -> "PathPolicy":
        return cls(settings.allowed_roots, settings.sandbox_dir, **kw)  # type: ignore[arg-type]

    def check(self, raw: str) -> PathCheck:
        def fail(reason: ReasonCode, detail: str) -> PathCheck:
            return PathCheck(ok=False, reason=reason, raw=raw, resolved=resolved, detail=detail)

        resolved: Path | None = None
        if (
            not raw.strip()
            or len(raw) > MAX_PATH_CHARS
            or any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) == "Cf" for c in raw)
            or unicodedata.normalize("NFKC", raw) != raw
        ):
            return fail(ReasonCode.PATH_INVALID, "empty, too long, control or odd characters")
        try:
            expanded = _expand(raw, self._env, self._home)
            _validate_syntax(expanded)
            if HARD_DENY_DRIVE_ROOTS and _is_drive_root(os.path.normpath(expanded)):
                return fail(ReasonCode.PATH_HARD_DENY, "drive root")
            resolved = _real(expanded)
        except _Invalid as exc:
            return fail(ReasonCode.PATH_INVALID, str(exc))
        except (OSError, ValueError):
            return fail(ReasonCode.PATH_INVALID, "path resolution failed")

        if HARD_DENY_DRIVE_ROOTS and _is_drive_root(str(resolved)):
            return fail(ReasonCode.PATH_HARD_DENY, "drive root")
        if any(_inside(resolved, d) for d in self._hard_dirs):
            return fail(ReasonCode.PATH_HARD_DENY, "protected directory")
        for comp in resolved.parts[1:]:
            low = comp.casefold()
            if any(fnmatch.fnmatchcase(low, pat.casefold()) for pat in HARD_DENY_FILE_PATTERNS):
                return fail(ReasonCode.PATH_DENIED_FILE_PATTERN, "protected file name")
        if not any(_inside(resolved, r) for r in self._roots):
            return fail(ReasonCode.PATH_OUTSIDE_ALLOWED_ROOTS, "outside allowed roots")
        return PathCheck(ok=True, reason=None, raw=raw, resolved=resolved, detail="allowed")

    def check_all(self, raws: Iterable[str]) -> PathCheck:
        last: PathCheck | None = None
        for raw in raws:
            last = self.check(raw)
            if not last.ok:
                return last
        return last or PathCheck(ok=True, reason=None, raw="", resolved=None, detail="no paths")
