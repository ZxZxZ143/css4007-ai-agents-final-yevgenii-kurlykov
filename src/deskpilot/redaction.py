"""Secret redaction and size bounds for trace payloads.

Redaction runs on full strings before truncation, so a secret cut in half cannot leak.
"""

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"
DEPTH_MARKER = "[depth limit]"

MAX_STRING = 2000
MAX_ITEMS = 50
MAX_KEYS = 100
MAX_DEPTH = 6
MAX_LINE_BYTES = 16 * 1024
MIN_SECRET_LEN = 4

_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "private_key",
    "credential",
)
# Keys containing "token" keep numbers/bool/None so usage counters (input_tokens, max_tokens,
# budgets.tokens) survive. A mapping passes only under a counter-container key (..tokens/..usage).
_TOKEN_KEY_PART = "token"
_COUNTER_CONTAINER_SUFFIXES = ("tokens", "usage")
_PRIVATE_KEY_PARTS = ("reasoning", "thinking", "chain_of_thought", "thought", "scratchpad")
MAX_INPUT_CHARS = 4 * MAX_STRING

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        REDACTED,
    ),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}"), REDACTED),
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),
    (
        re.compile(r"(?i)\b(password|passwd|pwd|api_?key|token|secret)=[^&\s\"']+"),
        rf"\1={REDACTED}",
    ),
)


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def _scrub_text(text: str, secrets: tuple[str, ...]) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def _bound_text(text: str) -> str:
    if len(text) <= MAX_STRING:
        return text
    return f"{text[:MAX_STRING]}…[truncated {len(text) - MAX_STRING} chars]"


def _clean_text(text: str, secrets: tuple[str, ...]) -> str:
    """Scrub then truncate; input is capped first so huge strings stay cheap."""
    cut = text[:MAX_INPUT_CHARS]
    scrubbed = _scrub_text(cut, secrets)
    dropped = len(text) - len(cut)
    if dropped == 0:
        return _bound_text(scrubbed)
    return (
        f"{scrubbed[:MAX_STRING]}…[truncated {dropped + max(0, len(scrubbed) - MAX_STRING)} chars]"
    )


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _walk(value: Any, secrets: tuple[str, ...], depth: int) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return _clean_text(str(value), secrets)
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        if depth >= MAX_DEPTH:
            return DEPTH_MARKER
        if isinstance(value, Mapping):
            return _walk_mapping(value, secrets, depth)
        items = list(value)
        out = [_walk(item, secrets, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"[{len(items) - MAX_ITEMS} more items omitted]")
        return out
    return _clean_text(_safe_repr(value), secrets)


def _is_secret_key(norm: str, item: Any) -> bool:
    if any(part in norm for part in _SECRET_KEY_PARTS):
        return True
    if _TOKEN_KEY_PART not in norm:
        return False
    if item is None or isinstance(item, bool | int | float):
        return False
    if isinstance(item, Mapping):
        return not norm.endswith(_COUNTER_CONTAINER_SUFFIXES)
    return True


def _unique_key(key: str, out: Mapping[str, Any]) -> str:
    candidate, n = key, 1
    while candidate in out:
        n += 1
        candidate = f"{key}#{n}"
    return candidate


def _walk_mapping(value: Mapping[Any, Any], secrets: tuple[str, ...], depth: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for count, (raw_key, item) in enumerate(value.items()):
        if count >= MAX_KEYS:
            out["_omitted_keys"] = len(value) - MAX_KEYS
            break
        norm = _normalize_key(str(raw_key))
        key = _unique_key(_clean_text(str(raw_key), secrets), out)
        if any(part in norm for part in _PRIVATE_KEY_PARTS):
            out[key] = OMITTED
        elif _is_secret_key(norm, item):
            out[key] = REDACTED
        else:
            out[key] = _walk(item, secrets, depth + 1)
    return out


def redact(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    """Return a JSON-safe, redacted and bounded copy of value."""
    usable = tuple(sorted({s for s in secrets if len(s) >= MIN_SECRET_LEN}, key=len, reverse=True))
    return _walk(value, usable, 0)
