"""Fast-path router: deterministic handling of simple single-intent "open" requests.

The router only builds a gate-ready ActionRequest. It never executes anything,
never grants permission and does no I/O. When unsure it returns `model`.
"""

import ipaddress
import re
import unicodedata
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Final
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, model_validator

from deskpilot.policy.types import ActionRequest, ProvenanceFlag, RiskClass
from deskpilot.settings import Settings

TOOL_APP_OPEN: Final = "app.open"
TOOL_FILES_OPEN: Final = "files.open"
TOOL_URL_OPEN: Final = "browser.open_url"

MAX_GOAL_LENGTH: Final = 200
MAX_URL_LENGTH: Final = 500
MAX_SUMMARY_LENGTH: Final = 200

SAFE_OPEN_SUFFIXES: Final = frozenset(
    {
        ".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
        ".csv", ".docx", ".xlsx", ".pptx", ".odt", ".mp3", ".wav", ".mp4", ".mkv",
    }
)  # fmt: skip

_BARE_TLDS: Final = frozenset(
    [
        "com",
        "net",
        "org",
        "ru",
        "ua",
        "by",
        "kz",
        "io",
        "dev",
        "app",
        "info",
        "edu",
        "gov",
        "uk",
        "de",
        "fr",
        "eu",
        "me",
        "tv",
        "co",
    ]
)
_DOS_COM_NAMES: Final = frozenset(
    [
        "setup",
        "install",
        "installer",
        "update",
        "run",
        "start",
        "cmd",
        "command",
        "config",
        "format",
        "edit",
        "debug",
        "mode",
        "more",
    ]
)
_LOCAL_HOST_SUFFIXES: Final = (
    ".localhost", ".local", ".internal", ".lan", ".home.arpa", ".arpa", ".intranet", ".corp",
)  # fmt: skip
_NUMERIC_LABEL: Final = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_DENIED_APPS: Final = frozenset(
    [
        "cmd",
        "powershell",
        "pwsh",
        "wt",
        "regedit",
        "mmc",
        "wscript",
        "cscript",
        "mshta",
        "rundll32",
        "regsvr32",
        "msiexec",
        "control",
        "taskmgr",
        "bash",
        "wsl",
        "python",
        "node",
        "certutil",
        "bitsadmin",
        "schtasks",
        "sc",
        "net",
        "reg",
        "diskpart",
        "format",
        "bcdedit",
    ]
)
_RESERVED_DEVICES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_OPEN_VERBS: Final = frozenset({"открой", "откройте", "открыть", "open"})
_APP_ONLY_VERBS: Final = frozenset(
    {"запусти", "запустите", "запустить", "включи", "launch", "start", "run"}
)

_APP_ALIASES: Final = {
    "блокнот": "notepad",
    "блокнотик": "notepad",
    "notepad": "notepad",
    "калькулятор": "calc",
    "calculator": "calc",
    "calc": "calc",
    "проводник": "explorer",
    "explorer": "explorer",
    "file explorer": "explorer",
}

_FOLDER_ALIASES: Final = {
    "рабочий стол": "Desktop",
    "рабочем столе": "Desktop",
    "desktop": "Desktop",
    "документы": "Documents",
    "мои документы": "Documents",
    "documents": "Documents",
    "загрузки": "Downloads",
    "загрузок": "Downloads",
    "downloads": "Downloads",
    "изображения": "Pictures",
    "картинки": "Pictures",
    "pictures": "Pictures",
    "музыка": "Music",
    "музыку": "Music",
    "music": "Music",
    "видео": "Videos",
    "videos": "Videos",
}
_SANDBOX_WORDS: Final = frozenset({"песочницу", "sandbox"})

_FILLER: Final = r"(?:пожалуйста|плиз|please|pls|мне)"
_FOLDER_PREFIX: Final = re.compile(r"^(?:папку|папка|folder|the)\s+")
_QUOTES: Final = "\"'«»“”"
_DRIVE_PATH: Final = re.compile(r"^[A-Za-z]:[\\/]")
_BARE_DOMAIN: Final = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.([a-z]{2,})(?:[/?#]\S*)?$", re.I)
_HOST: Final = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
_PATH_BAD_CHARS: Final = frozenset('<>"|?*%:')


class RouteKind(StrEnum):
    FAST_PATH = "fast_path"
    MODEL = "model"


class RouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: RouteKind
    action: ActionRequest | None = None
    intent: str | None = None
    reason: str

    @model_validator(mode="after")
    def _kind_matches_payload(self) -> "RouteDecision":
        has_payload = self.action is not None and self.intent is not None
        no_payload = self.action is None and self.intent is None
        if (self.kind is RouteKind.FAST_PATH and not has_payload) or (
            self.kind is RouteKind.MODEL and not no_payload
        ):
            raise ValueError("fast_path requires action and intent; model forbids them")
        return self


class _Target(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent: str
    tool: str
    args: dict[str, str]
    paths: tuple[str, ...] = ()
    summary: str


def _model(reason: str) -> RouteDecision:
    return RouteDecision(kind=RouteKind.MODEL, reason=reason)


def _fold(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _resolve_app(obj: str, settings: Settings) -> _Target | None:
    allowed = {entry.lower(): entry for entry in settings.app_allowlist}
    key = _fold(obj)
    app_id = _APP_ALIASES.get(key, key)
    if app_id not in allowed or app_id.removesuffix(".exe") in _DENIED_APPS:
        return None
    app = allowed[app_id]
    return _Target(
        intent="open_app",
        tool=TOOL_APP_OPEN,
        args={"app": app},
        summary=f"Открыть приложение: {app}",
    )


def _resolve_folder(obj: str, settings: Settings) -> _Target | None:
    key = _fold(obj)
    while (match := _FOLDER_PREFIX.match(key)) is not None:
        key = key[match.end() :]
    folder = None
    if key in _SANDBOX_WORDS:
        folder = settings.sandbox_dir
    elif key in _FOLDER_ALIASES:
        name = _FOLDER_ALIASES[key].lower()
        folder = next((r for r in settings.allowed_roots if r.name.lower() == name), None)
    if folder is None:
        return None
    path = str(folder)
    return _Target(
        intent="open_folder",
        tool=TOOL_FILES_OPEN,
        args={"path": path},
        paths=(path,),
        summary=f"Открыть папку: {path}",
    )


def _resolve_path(obj: str, quoted: bool) -> _Target | None:
    if not _DRIVE_PATH.match(obj) or (not quoted and any(c.isspace() for c in obj)):
        return None
    if any(c in _PATH_BAD_CHARS for c in obj[2:]) or any(ord(c) < 32 for c in obj):
        return None
    parts = re.split(r"[\\/]+", obj[3:])
    if not obj[3:].strip("\\/") or any(p in {"", ".", ".."} or p != p.strip() for p in parts[:-1]):
        return None
    if any(p.split(".")[0].lower() in _RESERVED_DEVICES for p in parts):
        return None
    last = parts[-1]
    if last != last.strip() or last in {".", ".."} or last.endswith("."):
        return None
    suffix = PureWindowsPath(obj).suffix.lower()
    if suffix and suffix not in SAFE_OPEN_SUFFIXES:
        return None
    label = "файл" if suffix else "папку"
    return _Target(
        intent="open_path",
        tool=TOOL_FILES_OPEN,
        args={"path": obj},
        paths=(obj,),
        summary=f"Открыть {label}: {obj}",
    )


def _resolve_url(obj: str) -> _Target | None:
    if len(obj) > MAX_URL_LENGTH or any(c.isspace() or c == "\\" or ord(c) < 32 for c in obj):
        return None
    if re.match(r"^https?://", obj, re.I):
        candidate = obj
    else:
        match = _BARE_DOMAIN.match(obj)
        if match is None or match.group(1).lower() not in _BARE_TLDS:
            return None
        host_part = re.split(r"[/?#]", obj, maxsplit=1)[0].lower()
        if host_part.count(".") == 1 and host_part.split(".")[0] in _DOS_COM_NAMES:
            return None
        candidate = f"https://{obj}"
    try:
        parsed = urlparse(candidate)
        _ = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if "@" in parsed.netloc or not host.isascii() or not _HOST.match(host):
        return None
    labels = host.split(".")
    if (
        host.endswith(_LOCAL_HOST_SUFFIXES)
        or any(_NUMERIC_LABEL.match(label) for label in labels)
        or any(label.startswith("xn--") for label in labels)
    ):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    url = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()).geturl()
    return _Target(
        intent="open_url",
        tool=TOOL_URL_OPEN,
        args={"url": url},
        summary=f"Открыть ссылку: {url}",
    )


def _strip_object(rest: str) -> tuple[str, bool]:
    """Remove one pair of surrounding quotes. Any other quote means: unsure."""
    quoted = len(rest) >= 2 and rest[0] in _QUOTES and rest[-1] in _QUOTES
    if quoted:
        rest = rest[1:-1].strip()
    if any(c in _QUOTES for c in rest):
        return "", quoted
    return rest, quoted


def route(goal: str, settings: Settings, run_id: str) -> RouteDecision:
    """Return a gate-ready READ action for a simple "open" request, else `model`."""
    if not goal.strip() or len(goal) > MAX_GOAL_LENGTH:
        return _model("empty or too long")
    if any(c in goal for c in "\n\r\t;|`") or "&&" in goal:
        return _model("compound request")
    if unicodedata.normalize("NFKC", goal) != goal or any(
        unicodedata.category(c) in {"Cf", "Cc"} for c in goal
    ):
        return _model("suspicious characters")

    rest = goal.strip()
    wake = "|".join(re.escape(w.lower()) for w in {"deskpilot", settings.wake_phrase})
    rest = re.sub(rf"^(?:{wake})(?:\s*,\s*|\s+)", "", rest, flags=re.I)
    if "," in rest:
        return _model("compound request")
    rest = re.sub(r"[.!?]+$", "", rest).strip()
    while (m := re.match(rf"^{_FILLER}\s+", rest, re.I)) is not None:
        rest = rest[m.end() :]

    head = re.match(r"^(\S+?)(?:-ка)?(?:\s+|$)", rest)
    if head is None:
        return _model("no verb")
    verb = head.group(1).lower().replace("ё", "е")
    if verb not in _OPEN_VERBS | _APP_ONLY_VERBS:
        return _model("no open verb")
    rest = rest[head.end() :]
    while (m := re.match(rf"^{_FILLER}\s+", rest, re.I)) is not None:
        rest = rest[m.end() :]
    while (m := re.search(rf"\s+{_FILLER}$", rest, re.I)) is not None:
        rest = rest[: m.start()]

    obj, quoted = _strip_object(rest.strip())
    if not obj:
        return _model("no clean object")

    target = _resolve_app(obj, settings)
    if target is None and verb in _OPEN_VERBS:
        target = _resolve_folder(obj, settings) or _resolve_path(obj, quoted) or _resolve_url(obj)
    if target is None:
        return _model("unrecognized object")

    action = ActionRequest(
        run_id=run_id,
        tool=target.tool,
        risk_class=RiskClass.READ,
        args=target.args,
        paths=target.paths,
        provenance=frozenset({ProvenanceFlag.FAST_PATH}),
        summary=target.summary[:MAX_SUMMARY_LENGTH],
    )
    return RouteDecision(
        kind=RouteKind.FAST_PATH,
        action=action,
        intent=target.intent,
        reason=f"matched {target.intent}:{next(iter(target.args.values()))}"[:100],
    )
