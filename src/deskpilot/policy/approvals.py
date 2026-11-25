"""Action-bound, single-use, expiring approvals with text and voice (challenge code) channels.

Resolution comes only from the user's channel through resolve_text/resolve_voice.
Tool output, web pages and model text have no API here that can approve anything.

Voice rules: the challenge code is shown on screen only (display_text) and is never part of
spoken_prompt, so a TTS echo into the microphone cannot approve itself. A transcript approves
only if it holds a confirmation word and exactly one number, equal to the code. Any refusal
word anywhere in the transcript denies (the safe direction). Expiry is latched and the clock
is treated as monotonic, so a clock moving backwards cannot revive an approval.
"""

from __future__ import annotations

import math
import re
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.policy.types import ActionRequest, ApprovalChannel

TEXT_TTL_S: Final = 120
VOICE_TTL_S: Final = 30
VOICE_MIN_CONFIDENCE: Final = 0.8
SUMMARY_MAX: Final = 200


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class VoiceResult(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    FALLBACK_TO_TEXT = "fallback_to_text"


class _Rng(Protocol):
    def randint(self, a: int, b: int) -> int: ...


_RU_UNITS: Final = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_RU_TEENS: Final = (
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_RU_TENS: Final = (
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
)
_EN_UNITS: Final = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_EN_TEENS: Final = (
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_EN_TENS: Final = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")

_CONFIRM_WORDS: Final = frozenset({"подтверждаю", "confirm", "approve"})
_REFUSAL_WORDS: Final = frozenset({"нет", "отмена", "отменить", "стоп", "no", "cancel", "stop"})


def _word_tables(
    units: tuple[str, ...], teens: tuple[str, ...], tens: tuple[str, ...]
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (words for 10..19 and the tens, unit words 1..9)."""
    head = {w: 10 + i for i, w in enumerate(teens)}
    head.update({w: 20 + 10 * i for i, w in enumerate(tens)})
    return head, {w: i for i, w in enumerate(units) if w}


_RU_HEAD, _RU_UNIT = _word_tables(_RU_UNITS, _RU_TEENS, _RU_TENS)
_EN_HEAD, _EN_UNIT = _word_tables(_EN_UNITS, _EN_TEENS, _EN_TENS)
_HEAD: Final = {**_RU_HEAD, **_EN_HEAD}
_UNIT: Final = {**_RU_UNIT, **_EN_UNIT}


def number_to_russian(n: int) -> str:
    """Spell 10..99 in Russian."""
    if not 10 <= n <= 99:
        raise ValueError("number must be in 10..99")
    if n < 20:
        return _RU_TEENS[n - 10]
    tens, unit = divmod(n, 10)
    return " ".join(w for w in (_RU_TENS[tens - 2], _RU_UNITS[unit]) if w)


def _tokens(text: str) -> list[str]:
    cleaned = text.casefold().replace("ё", "е")
    return re.findall(r"\d+|[^\W\d_]+", cleaned)


_OTHER_NUMBER_WORDS: Final = frozenset(
    [
        "ноль",
        "сто",
        "двести",
        "триста",
        "четыреста",
        "пятьсот",
        "шестьсот",
        "семьсот",
        "восемьсот",
        "девятьсот",
        "тысяча",
        "тысячи",
        "тысяч",
        "zero",
        "hundred",
        "thousand",
        "million",
    ]
)


def extract_numbers(text: str) -> list[int]:
    """All numbers in the text, in order: digits or RU/EN words ("сорок семь", "forty-seven").

    Words outside 1..99 (hundreds, thousands, zero) count as a number (-1) so that
    "сто сорок семь" is never read as 47.
    """
    found: list[int] = []
    tokens = _tokens(text)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok.isdigit():
            found.append(int(tok))
        elif tok in _HEAD:
            value = _HEAD[tok]
            if value >= 20 and nxt in _UNIT:
                value += _UNIT[nxt]
                i += 1
            found.append(value)
        elif tok in _UNIT:
            found.append(_UNIT[tok])
        elif tok in _OTHER_NUMBER_WORDS:
            found.append(-1)
        i += 1
    return found


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = Field(min_length=1)
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX)
    channel: ApprovalChannel
    created_at: datetime
    expires_at: datetime
    challenge_code: str | None = None
    spoken_prompt: str = Field(min_length=1)  # safe for TTS: never contains the code
    display_text: str = Field(min_length=1)  # screen only: contains the code for voice

    @model_validator(mode="after")
    def _consistent(self) -> ApprovalRequest:
        if self.channel is ApprovalChannel.NONE:
            raise ValueError("approval channel must be voice or text")
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        has_code = self.challenge_code is not None
        if has_code != (self.channel is ApprovalChannel.VOICE):
            raise ValueError("challenge_code is required for voice and forbidden for text")
        if has_code and not re.fullmatch(r"[1-9][0-9]", self.challenge_code or ""):
            raise ValueError("challenge_code must be 10..99")
        if self.challenge_code and self.challenge_code in self.spoken_prompt:
            raise ValueError("spoken_prompt must not contain the challenge code")
        return self


@dataclass
class _Entry:
    request: ApprovalRequest
    status: ApprovalStatus = ApprovalStatus.PENDING


def _text_prompt(summary: str) -> str:
    return f"Подтвердить: {summary}?"


_VOICE_SPOKEN: Final = "Назовите код подтверждения с экрана"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApprovalStore:
    """In-memory approval registry. Thread-safe (single lock); keeps no raw args."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: _Rng | None = None,
    ) -> None:
        self._clock = clock or _utc_now
        self._rng: _Rng = rng or secrets.SystemRandom()
        self._last_seen: datetime | None = None
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def request(
        self, action: ActionRequest, channel: ApprovalChannel, summary: str | None = None
    ) -> ApprovalRequest:
        """Create a pending approval; replaces (invalidates) a pending one for the same action."""
        if channel not in (ApprovalChannel.VOICE, ApprovalChannel.TEXT):
            raise ValueError("approval channel must be voice or text")
        text = (summary or action.summary).strip() or f"{action.tool} {action.canonical_args()}"
        text = text[:SUMMARY_MAX]
        fingerprint = action.fingerprint()
        with self._lock:
            now = self._now()
            for entry in self._entries.values():
                old = entry.request
                if (
                    self._effective(entry, now) is ApprovalStatus.PENDING
                    and old.run_id == action.run_id
                    and old.action_fingerprint == fingerprint
                ):
                    entry.status = ApprovalStatus.EXPIRED  # superseded by the new request
            if channel is ApprovalChannel.VOICE:
                code = str(self._rng.randint(10, 99))
                ttl, spoken = VOICE_TTL_S, _VOICE_SPOKEN
                display = f"Скажите: подтверждаю, {code}"
            else:
                code, ttl = None, TEXT_TTL_S
                spoken = display = _text_prompt(text)
            req = ApprovalRequest(
                run_id=action.run_id,
                action_fingerprint=fingerprint,
                tool=action.tool,
                summary=text,
                channel=channel,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl),
                challenge_code=code,
                spoken_prompt=spoken,
                display_text=display,
            )
            self._entries[req.approval_id] = _Entry(req)
            return req

    def get(self, approval_id: str) -> ApprovalRequest:
        """Current request (channel, expiry and prompt change after a voice fallback)."""
        with self._lock:
            return self._entry(approval_id).request

    def status(self, approval_id: str) -> ApprovalStatus:
        with self._lock:
            return self._effective(self._entry(approval_id), self._now())

    def resolve_text(self, approval_id: str, approved: bool) -> ApprovalStatus:
        """Approve or deny a pending request; otherwise return the unchanged status."""
        with self._lock:
            entry = self._entry(approval_id)
            current = self._effective(entry, self._now())
            if current is ApprovalStatus.PENDING:
                entry.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
                return entry.status
            return current

    def resolve_voice(self, approval_id: str, transcript: str, confidence: float) -> VoiceResult:
        """Check a spoken answer. Anything but a clear match falls back to the text channel."""
        with self._lock:
            entry = self._entry(approval_id)
            now = self._now()
            req = entry.request
            if (
                self._effective(entry, now) is not ApprovalStatus.PENDING
                or req.channel is not ApprovalChannel.VOICE
            ):
                return VoiceResult.FALLBACK_TO_TEXT
            if not (math.isfinite(confidence) and VOICE_MIN_CONFIDENCE <= confidence <= 1):
                return self._to_text(entry, now)
            tokens = _tokens(transcript)
            words = set(tokens)
            if words & _REFUSAL_WORDS:
                entry.status = ApprovalStatus.DENIED
                return VoiceResult.DENIED
            if " ".join(_tokens(req.spoken_prompt)) in " ".join(tokens):
                return self._to_text(entry, now)  # echo of our own prompt
            numbers = extract_numbers(transcript)
            if words & _CONFIRM_WORDS and numbers == [int(req.challenge_code or 0)]:
                entry.status = ApprovalStatus.APPROVED
                return VoiceResult.APPROVED
            return self._to_text(entry, now)

    def consume(self, approval_id: str, action: ActionRequest) -> bool:
        """True exactly once for an approved, unexpired approval bound to this exact action.

        A mismatching action returns False and changes nothing, so a still-valid
        approval is neither burned nor usable for the other action.
        """
        with self._lock:
            entry = self._entries.get(approval_id)
            if entry is None:
                return False
            if self._effective(entry, self._now()) is not ApprovalStatus.APPROVED:
                return False
            req = entry.request
            if action.run_id != req.run_id or action.fingerprint() != req.action_fingerprint:
                return False
            entry.status = ApprovalStatus.CONSUMED
            return True

    def _entry(self, approval_id: str) -> _Entry:
        try:
            return self._entries[approval_id]
        except KeyError:
            raise KeyError(f"unknown approval {approval_id!r}") from None

    def _now(self) -> datetime:
        """Clock reading, never earlier than any earlier reading."""
        now = self._clock()
        if self._last_seen is None or now > self._last_seen:
            self._last_seen = now
        return self._last_seen

    @staticmethod
    def _effective(entry: _Entry, now: datetime) -> ApprovalStatus:
        live = entry.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        if live and now >= entry.request.expires_at:
            entry.status = ApprovalStatus.EXPIRED  # latched
        return entry.status

    @staticmethod
    def _to_text(entry: _Entry, now: datetime) -> VoiceResult:
        entry.request = entry.request.model_copy(
            update={
                "channel": ApprovalChannel.TEXT,
                "challenge_code": None,
                "expires_at": now + timedelta(seconds=TEXT_TTL_S),
                "spoken_prompt": _text_prompt(entry.request.summary),
                "display_text": _text_prompt(entry.request.summary),
            }
        )
        return VoiceResult.FALLBACK_TO_TEXT
