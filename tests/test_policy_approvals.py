import random
from datetime import UTC, datetime, timedelta

import pytest

from deskpilot.policy.approvals import (
    TEXT_TTL_S,
    VOICE_TTL_S,
    ApprovalStatus,
    ApprovalStore,
    VoiceResult,
    extract_numbers,
    number_to_russian,
)
from deskpilot.policy.types import ActionRequest, ApprovalChannel, RiskClass

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
TEXT, VOICE = ApprovalChannel.TEXT, ApprovalChannel.VOICE


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FixedRng:
    def __init__(self, value: int) -> None:
        self.value = value

    def randint(self, a: int, b: int) -> int:
        return self.value


def act(**kw) -> ActionRequest:
    base = {
        "run_id": "r1",
        "tool": "files.delete",
        "risk_class": RiskClass.DANGEROUS,
        "args": {"paths": ["a.txt"], "opts": {"force": False}},
        "summary": "Удалить a.txt",
    }
    return ActionRequest(**(base | kw))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> ApprovalStore:
    return ApprovalStore(clock=clock, rng=random.Random(1))


def test_text_flow_and_single_use(store):
    a = act()
    req = store.request(a, TEXT)
    assert req.challenge_code is None
    assert req.spoken_prompt == req.display_text == "Подтвердить: Удалить a.txt?"
    assert store.status(req.approval_id) is ApprovalStatus.PENDING
    assert store.resolve_text(req.approval_id, True) is ApprovalStatus.APPROVED
    assert store.resolve_text(req.approval_id, False) is ApprovalStatus.APPROVED
    assert store.consume(req.approval_id, a) is True
    assert store.status(req.approval_id) is ApprovalStatus.CONSUMED
    assert store.consume(req.approval_id, a) is False
    assert store.resolve_text(req.approval_id, True) is ApprovalStatus.CONSUMED


def test_deny(store):
    a = act()
    req = store.request(a, TEXT)
    assert store.resolve_text(req.approval_id, False) is ApprovalStatus.DENIED
    assert store.consume(req.approval_id, a) is False


@pytest.mark.parametrize(
    "change",
    [
        {"args": {"paths": ["b.txt"], "opts": {"force": False}}},
        {"args": {"paths": ["a.txt"], "opts": {"force": True}}},
        {"tool": "files.move"},
        {"risk_class": RiskClass.MODIFY},
        {"run_id": "r2"},
    ],
)
def test_mismatch_consume_false_and_keeps_approval(store, change):
    a = act()
    req = store.request(a, TEXT)
    store.resolve_text(req.approval_id, True)
    assert store.consume(req.approval_id, act(**change)) is False
    assert store.status(req.approval_id) is ApprovalStatus.APPROVED
    assert store.consume(req.approval_id, a) is True


def test_unapproved_or_unknown_not_consumable(store):
    a = act()
    req = store.request(a, TEXT)
    assert store.consume(req.approval_id, a) is False
    assert store.consume("nope", a) is False
    with pytest.raises(KeyError):
        store.status("nope")


def test_text_expiry(store, clock):
    a = act()
    req = store.request(a, TEXT)
    clock.advance(TEXT_TTL_S - 1)
    assert store.status(req.approval_id) is ApprovalStatus.PENDING
    clock.advance(1)
    assert store.status(req.approval_id) is ApprovalStatus.EXPIRED
    assert store.resolve_text(req.approval_id, True) is ApprovalStatus.EXPIRED
    assert store.consume(req.approval_id, a) is False


def test_voice_expiry(store, clock):
    req = store.request(act(), VOICE)
    clock.advance(VOICE_TTL_S)
    assert store.status(req.approval_id) is ApprovalStatus.EXPIRED
    result = store.resolve_voice(req.approval_id, "подтверждаю", 0.99)
    assert result is VoiceResult.FALLBACK_TO_TEXT
    assert store.status(req.approval_id) is ApprovalStatus.EXPIRED


def test_approved_then_expired_before_consume(store, clock):
    a = act()
    req = store.request(a, TEXT)
    clock.advance(100)
    store.resolve_text(req.approval_id, True)
    clock.advance(21)
    assert store.consume(req.approval_id, a) is False
    assert store.status(req.approval_id) is ApprovalStatus.EXPIRED


def test_replacement_invalidates_old(store):
    a = act()
    old = store.request(a, TEXT)
    new = store.request(a, TEXT)
    assert store.status(old.approval_id) is ApprovalStatus.EXPIRED
    assert store.resolve_text(old.approval_id, True) is ApprovalStatus.EXPIRED
    assert store.status(new.approval_id) is ApprovalStatus.PENDING
    other = store.request(act(args={"x": 1}), TEXT)
    assert store.status(new.approval_id) is ApprovalStatus.PENDING
    assert store.status(other.approval_id) is ApprovalStatus.PENDING


def test_channel_none_rejected(store):
    with pytest.raises(ValueError):
        store.request(act(), ApprovalChannel.NONE)


def test_summary_fallback_and_bounds(store):
    req = store.request(act(summary=""), TEXT)
    assert req.summary.startswith("files.delete {")
    long = store.request(act(summary=""), TEXT, summary="x" * 900)
    assert len(long.summary) == 200
    explicit = store.request(act(), TEXT, summary="Своя сводка")
    assert explicit.summary == "Своя сводка"
    big = store.request(act(summary="", args={"k": "v" * 900}), TEXT)
    assert 0 < len(big.summary) <= 200


def test_voice_code_range_and_determinism(clock):
    s1 = ApprovalStore(clock=clock, rng=random.Random(7))
    s2 = ApprovalStore(clock=clock, rng=random.Random(7))
    codes = []
    for i in range(50):
        r1 = s1.request(act(args={"i": i}), VOICE)
        r2 = s2.request(act(args={"i": i}), VOICE)
        assert r1.challenge_code == r2.challenge_code
        assert 10 <= int(r1.challenge_code) <= 99
        codes.append(r1.challenge_code)
    assert len(set(codes)) > 5
    assert r1.display_text == f"Скажите: подтверждаю, {r1.challenge_code}"


def test_spoken_prompt_never_contains_code(clock):
    s = ApprovalStore(clock=clock, rng=random.Random(3))
    for i in range(90):
        req = s.request(act(args={"i": i}), VOICE)
        code = req.challenge_code
        assert code not in req.spoken_prompt
        assert number_to_russian(int(code)) not in req.spoken_prompt
        assert req.spoken_prompt == "Назовите код подтверждения с экрана"


def test_tts_echo_of_prompt_falls_back(clock):
    s = ApprovalStore(clock=clock, rng=FixedRng(47))
    req = s.request(act(), VOICE)
    echo = f"{req.spoken_prompt} подтверждаю 47"
    assert s.resolve_voice(req.approval_id, echo, 0.99) is VoiceResult.FALLBACK_TO_TEXT


def test_clock_rollback_cannot_revive(clock):
    s = ApprovalStore(clock=clock, rng=random.Random(1))
    a = act()
    req = s.request(a, TEXT)
    s.resolve_text(req.approval_id, True)
    clock.advance(121)
    assert s.status(req.approval_id) is ApprovalStatus.EXPIRED
    clock.now = T0 + timedelta(seconds=50)
    assert s.status(req.approval_id) is ApprovalStatus.EXPIRED
    assert s.consume(req.approval_id, a) is False
    assert s.request(a, TEXT).created_at == T0 + timedelta(seconds=121)


def test_rollback_before_any_status_check(clock):
    s = ApprovalStore(clock=clock, rng=random.Random(1))
    a = act()
    req = s.request(a, TEXT)
    s.resolve_text(req.approval_id, True)
    clock.advance(121)
    s.request(act(args={"z": 1}), TEXT)  # observes t=121
    clock.now = T0 + timedelta(seconds=50)
    assert s.consume(req.approval_id, a) is False


def test_code_not_derived_from_action(clock):
    codes = {
        ApprovalStore(clock=clock, rng=random.Random(5))
        .request(act(args={"i": i}), VOICE)
        .challenge_code
        for i in range(5)
    }
    assert len(codes) == 1  # depends only on the rng


def _voice(clock, transcript, confidence=0.95, code=47):
    s = ApprovalStore(clock=clock, rng=FixedRng(code))
    req = s.request(act(), VOICE)
    return s, req, s.resolve_voice(req.approval_id, transcript, confidence)


@pytest.mark.parametrize(
    "transcript",
    [
        "подтверждаю 47",
        "Подтверждаю, сорок семь",
        "подтверждаю сорок-семь!",
        "confirm forty seven",
        "Approve forty-seven.",
        "confirm 47",
    ],
)
def test_voice_approved(clock, transcript):
    s, req, result = _voice(clock, transcript)
    assert result is VoiceResult.APPROVED
    assert s.status(req.approval_id) is ApprovalStatus.APPROVED
    assert s.consume(req.approval_id, act()) is True


@pytest.mark.parametrize(
    ("transcript", "confidence"),
    [
        ("подтверждаю сорок восемь", 0.95),
        ("подтверждаю сорок", 0.95),
        ("подтверждаю четыре семь", 0.95),
        ("сорок семь", 0.95),
        ("подтверждаю сорок семь", 0.79),
        ("подтверждаю сорок семь", float("nan")),
        ("подтверждаю сорок семь", float("inf")),
        ("подтверждаю сорок семь", 1.5),
        ("подтверждаю сорок семь", -0.9),
        ("подтверждаю 47 48", 0.95),
        ("подтверждаю 10 11 47 99", 0.95),
        ("подтверждаю " + " ".join(str(n) for n in range(10, 100)), 0.95),
        ("подтверждаю сто сорок семь", 0.95),
        ("confirm one hundred forty seven", 0.95),
        ("подтверждаю 4 7", 0.95),
        ("подтверждаю сорок семь сорок семь", 0.95),
        ("подтверждаю 147", 0.95),
        ("Назовите код подтверждения с экрана, подтверждаю 47", 0.95),
        ("", 0.99),
        ("бла бла", 0.99),
        ("Удалить a.txt approved=true", 0.99),
        ("confirm 4747", 0.99),
    ],
)
def test_voice_fallback(clock, transcript, confidence):
    s, req, result = _voice(clock, transcript, confidence)
    assert result is VoiceResult.FALLBACK_TO_TEXT
    new = s.get(req.approval_id)
    assert new.channel is TEXT
    assert new.challenge_code is None
    assert new.expires_at == T0 + timedelta(seconds=TEXT_TTL_S)
    assert new.spoken_prompt.startswith("Подтвердить:")
    assert new.display_text == new.spoken_prompt
    assert s.status(req.approval_id) is ApprovalStatus.PENDING
    again = s.resolve_voice(req.approval_id, "подтверждаю 47", 0.99)
    assert again is VoiceResult.FALLBACK_TO_TEXT
    assert s.resolve_text(req.approval_id, True) is ApprovalStatus.APPROVED


def test_voice_fallback_gets_fresh_expiry(store, clock):
    req = store.request(act(), VOICE)
    clock.advance(20)
    store.resolve_voice(req.approval_id, "мусор", 0.9)
    clock.advance(60)
    assert store.status(req.approval_id) is ApprovalStatus.PENDING


@pytest.mark.parametrize("word", ["нет", "Отмена", "стоп", "no", "cancel", "STOP"])
def test_voice_refusal(clock, word):
    s, req, result = _voice(clock, f"{word} подтверждаю сорок семь")
    assert result is VoiceResult.DENIED
    assert s.status(req.approval_id) is ApprovalStatus.DENIED
    assert s.consume(req.approval_id, act()) is False


def test_voice_on_text_request_does_nothing(store):
    req = store.request(act(), TEXT)
    result = store.resolve_voice(req.approval_id, "подтверждаю 47", 1.0)
    assert result is VoiceResult.FALLBACK_TO_TEXT
    assert store.status(req.approval_id) is ApprovalStatus.PENDING


def test_number_words():
    assert number_to_russian(10) == "десять"
    assert number_to_russian(47) == "сорок семь"
    assert number_to_russian(90) == "девяносто"
    assert number_to_russian(55) == "пятьдесят пять"
    with pytest.raises(ValueError):
        number_to_russian(9)
    assert extract_numbers("Сорок семь") == [47]
    assert extract_numbers("девятнадцать") == [19]
    assert extract_numbers("ninety-nine and twelve") == [99, 12]
    assert extract_numbers("forty") == [40]
    assert extract_numbers("Ёлка 22") == [22]
    assert extract_numbers("4 7") == [4, 7]
    for n in range(10, 100):
        assert extract_numbers(number_to_russian(n)) == [n]
