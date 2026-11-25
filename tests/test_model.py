"""Offline tests for the model client."""

from __future__ import annotations

from pathlib import Path

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import SecretStr, ValidationError

from deskpilot.defaults import DEFAULT_MODEL_ID
from deskpilot.fake_model import ScriptedChatModel, ai_text, ai_tool_call
from deskpilot.model import (
    ModelAuthError,
    ModelBadRequestError,
    ModelClient,
    ModelConfig,
    ModelConfigError,
    ModelConnectionError,
    ModelError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
    create_openai_chat,
    create_openai_client,
    load_api_key,
)

FAKE_KEY = "sk-test-SECRET-123456"
REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
MSGS = [HumanMessage(content="hi")]


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def client(*script: AIMessage | Exception) -> tuple[ModelClient, ScriptedChatModel]:
    fake = ScriptedChatModel(script=list(script))
    return ModelClient(fake, ModelConfig()), fake


def status_exc(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    resp = httpx.Response(code, request=REQ)
    return cls(f"boom {FAKE_KEY}", response=resp, body=None)


def test_config_defaults_and_validation() -> None:
    cfg = ModelConfig()
    assert (cfg.model_id, cfg.timeout_s, cfg.max_retries) == (DEFAULT_MODEL_ID, 60, 2)
    for bad in ({"model_id": ""}, {"timeout_s": 0}, {"timeout_s": 301}, {"max_retries": 6}):
        with pytest.raises(ValidationError):
            ModelConfig(**bad)
    with pytest.raises(ValidationError):
        ModelConfig(unknown=1)


def test_text_response_and_usage() -> None:
    c, _ = client(ai_text("hello", input_tokens=3, output_tokens=4))
    r = c.invoke(MSGS)
    assert r.text == "hello" and r.tool_calls == ()
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.total_tokens) == (3, 4, 7)
    assert r.model_id == DEFAULT_MODEL_ID and r.latency_ms >= 0


def test_missing_usage_is_zero() -> None:
    c, _ = client(AIMessage(content="x"))
    assert c.invoke(MSGS).usage.total_tokens == 0


def test_tool_call_parsed_and_tools_recorded() -> None:
    @tool
    def get_current_time(timezone: str) -> str:
        """Return the time."""
        return timezone

    c, fake = client(ai_tool_call("get_current_time", {"timezone": "UTC"}, id="c1"))
    r = c.invoke(MSGS, tools=[get_current_time])
    assert fake.bind_calls == [{"tools": ["get_current_time"], "kwargs": {}}]
    assert len(r.tool_calls) == 1
    call = r.tool_calls[0]
    assert (call.name, call.args, call.id) == ("get_current_time", {"timezone": "UTC"}, "c1")


def test_multi_step_and_received_messages() -> None:
    c, fake = client(ai_tool_call("a", {}), ai_text("done"))
    assert c.invoke(MSGS).tool_calls[0].name == "a"
    assert c.invoke(MSGS).text == "done"
    assert len(fake.received) == 2 and fake.received[0][0].content == "hi"


def test_exhaustion_raises() -> None:
    c, _ = client(ai_text("only"))
    c.invoke(MSGS)
    with pytest.raises(ModelResponseError, match="exhausted"):
        c.invoke(MSGS)


def test_empty_and_invalid_tool_call_rejected() -> None:
    c, _ = client(AIMessage(content=""))
    with pytest.raises(ModelResponseError):
        c.invoke(MSGS)
    bad = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "x", "args": "{oops", "id": "1", "error": "bad", "type": "invalid_tool_call"}
        ],
    )
    c, _ = client(bad)
    with pytest.raises(ModelResponseError, match="invalid tool call"):
        c.invoke(MSGS)


@pytest.mark.parametrize(
    ("exc", "expected", "retryable"),
    [
        (status_exc(openai.AuthenticationError, 401), ModelAuthError, False),
        (status_exc(openai.PermissionDeniedError, 403), ModelAuthError, False),
        (status_exc(openai.RateLimitError, 429), ModelRateLimitError, True),
        (openai.APITimeoutError(request=REQ), ModelTimeoutError, True),
        (httpx.ReadTimeout(f"t {FAKE_KEY}"), ModelTimeoutError, True),
        (
            openai.APIConnectionError(message=f"c {FAKE_KEY}", request=REQ),
            ModelConnectionError,
            True,
        ),
        (status_exc(openai.BadRequestError, 400), ModelBadRequestError, False),
        (status_exc(openai.NotFoundError, 404), ModelConfigError, False),
        (status_exc(openai.InternalServerError, 503), ModelConnectionError, True),
        (status_exc(openai.UnprocessableEntityError, 422), ModelBadRequestError, False),
        (RuntimeError(f"weird {FAKE_KEY}"), ModelError, False),
    ],
)
def test_exception_mapping(exc: Exception, expected: type[ModelError], retryable: bool) -> None:
    c, _ = client(exc)
    with pytest.raises(ModelError) as info:
        c.invoke(MSGS)
    assert type(info.value) is expected
    assert info.value.retryable is retryable
    assert FAKE_KEY not in str(info.value)
    assert FAKE_KEY not in repr(info.value)


def quota_exc() -> openai.RateLimitError:
    resp = httpx.Response(429, request=REQ, headers={"x-request-id": "req_1"})
    return openai.RateLimitError(
        f"quota {FAKE_KEY}", response=resp, body={"code": "insufficient_quota"}
    )


def test_quota_not_retryable_and_diagnostics() -> None:
    c, _ = client(quota_exc())
    with pytest.raises(ModelRateLimitError) as info:
        c.invoke(MSGS)
    e = info.value
    assert e.retryable is False
    assert (e.status_code, e.request_id, e.code) == (429, "req_1", "insufficient_quota")
    assert FAKE_KEY not in str(e) and FAKE_KEY not in repr(e.__dict__)


@pytest.mark.parametrize(
    ("code", "expected"), [(408, ModelTimeoutError), (409, ModelConnectionError)]
)
def test_408_409_retryable(code: int, expected: type[ModelError]) -> None:
    c, _ = client(status_exc(openai.APIStatusError, code))
    with pytest.raises(ModelError) as info:
        c.invoke(MSGS)
    assert type(info.value) is expected and info.value.retryable is True
    assert info.value.status_code == code


def test_not_found_mentions_model_id() -> None:
    c, _ = client(status_exc(openai.NotFoundError, 404))
    with pytest.raises(ModelConfigError, match=DEFAULT_MODEL_ID):
        c.invoke(MSGS)


def test_model_error_passes_through() -> None:
    c, _ = client(ModelRateLimitError("slow down"))
    with pytest.raises(ModelRateLimitError, match="slow down"):
        c.invoke(MSGS)


def test_load_api_key_missing_and_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelConfigError, match="OPENAI_API_KEY"):
        load_api_key()
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    with pytest.raises(ModelConfigError):
        load_api_key()


def test_load_api_key_env_and_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"OPENAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    key = load_api_key(env)
    assert key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(key) and FAKE_KEY not in str(key)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert load_api_key(env).get_secret_value() == "sk-from-env"


def test_create_openai_client_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = ModelConfig(model_id="test-model", timeout_s=12, max_retries=1, temperature=0.2)
    c = create_openai_client(cfg, SecretStr(FAKE_KEY))
    chat = c._chat_model
    assert chat.model_name == "test-model"
    assert chat.request_timeout == 12
    assert chat.max_retries == 1
    assert chat.temperature == 0.2
    assert FAKE_KEY not in repr(c) and FAKE_KEY not in repr(cfg) and FAKE_KEY not in repr(chat)


def test_create_openai_client_defaults() -> None:
    c = create_openai_client(ModelConfig(), SecretStr(FAKE_KEY))
    assert c._chat_model.temperature is None
    assert c._chat_model.model_name == DEFAULT_MODEL_ID
    assert c._chat_model.use_responses_api is True
    assert c._chat_model.reasoning_effort is None


def test_create_openai_client_reasoning_and_transport() -> None:
    cfg = ModelConfig(use_responses_api=False, reasoning_effort="none")
    chat = create_openai_client(cfg, SecretStr(FAKE_KEY))._chat_model
    assert chat.use_responses_api is False
    assert chat.reasoning_effort == "none"
    with pytest.raises(ValueError):
        ModelConfig(reasoning_effort="max")


def test_text_ignores_reasoning_blocks() -> None:
    msg = AIMessage(
        content=[
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "secret plan"}]},
            {"type": "text", "text": "hello"},
        ]
    )
    model = ScriptedChatModel(script=[msg])
    resp = ModelClient(model, ModelConfig()).invoke([HumanMessage(content="hi")])
    assert resp.text == "hello"


def test_create_openai_chat_is_what_the_client_wraps() -> None:
    cfg = ModelConfig(model_id="chat-model", max_retries=0)
    chat = create_openai_chat(cfg, SecretStr(FAKE_KEY))
    assert chat.model_name == "chat-model"
    assert FAKE_KEY not in repr(chat)
    assert create_openai_client(cfg, SecretStr(FAKE_KEY)).config == cfg
