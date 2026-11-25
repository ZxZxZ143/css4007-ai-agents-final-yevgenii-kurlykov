"""Model client: configurable OpenAI chat model with typed errors and usage reporting.

ChatOpenAI already retries `max_retries` times internally. Callers must not add another retry
layer for the same call, except through the run's own retry budget.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
import openai
from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from deskpilot.defaults import DEFAULT_MODEL_ID

API_KEY_ENV = "OPENAI_API_KEY"


class ModelError(Exception):
    """Base class for model client failures. Messages never contain secrets."""

    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.code = code
        if retryable is not None:
            self.retryable = retryable


class ModelConfigError(ModelError):
    """Missing API key, unknown model or invalid configuration."""


class ModelAuthError(ModelError):
    """The provider rejected the credentials or permissions."""


class ModelRateLimitError(ModelError):
    retryable = True


class ModelTimeoutError(ModelError):
    retryable = True


class ModelConnectionError(ModelError):
    retryable = True


class ModelBadRequestError(ModelError):
    """The provider rejected the request."""


class ModelResponseError(ModelError):
    """Empty, malformed or unparseable model response."""


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(default=DEFAULT_MODEL_ID, min_length=1)
    timeout_s: float = Field(default=60, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    temperature: float | None = None
    # gpt-5.x reasoning models reject function tools on /v1/chat/completions unless
    # reasoning is off, so the Responses API is the default transport.
    use_responses_api: bool = True
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    args: dict[str, Any]
    id: str


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: TokenUsage = TokenUsage()
    model_id: str
    latency_ms: float


def load_api_key(env_file: Path | None = None) -> SecretStr:
    """Read OPENAI_API_KEY from the environment, optionally loading a .env first."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    value = os.environ.get(API_KEY_ENV, "").strip()
    if not value:
        raise ModelConfigError(
            f"{API_KEY_ENV} is not set. Put it in your local .env file or the environment."
        )
    return SecretStr(value)


def _map_exception(exc: Exception, model_id: str) -> ModelError:
    """Translate SDK exceptions into typed errors. Never copies the original text."""
    if isinstance(exc, ModelError):
        return exc
    diag: dict[str, Any] = {}
    if isinstance(exc, openai.APIStatusError):
        diag = {"status_code": exc.status_code, "request_id": exc.request_id}
    if isinstance(exc, openai.APIError) and isinstance(exc.code, str):
        diag["code"] = exc.code
    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException):
        return ModelTimeoutError("Model request timed out.", **diag)
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError):
        return ModelConnectionError("Could not connect to the model provider.", **diag)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ModelAuthError("The model provider rejected the credentials or permissions.", **diag)
    if isinstance(exc, openai.RateLimitError):
        if diag.get("code") == "insufficient_quota":
            return ModelRateLimitError(
                "The model provider quota is exhausted.", retryable=False, **diag
            )
        return ModelRateLimitError("The model provider rate limit was hit.", **diag)
    if isinstance(exc, openai.NotFoundError):
        return ModelConfigError(f"Model {model_id!r} was not found or is not available.", **diag)
    if isinstance(exc, openai.BadRequestError):
        return ModelBadRequestError(
            f"The model provider rejected the request for {model_id!r}.", **diag
        )
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 408:
            return ModelTimeoutError("The model provider timed out (HTTP 408).", **diag)
        if exc.status_code == 409 or exc.status_code >= 500:
            return ModelConnectionError(
                f"The model provider failed (HTTP {exc.status_code}).", **diag
            )
        return ModelBadRequestError(f"The model provider returned HTTP {exc.status_code}.", **diag)
    return ModelError(f"Unexpected model failure ({type(exc).__name__}).")


def _text_of(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block if isinstance(block, str) else str(block.get("text", ""))
        for block in content
        if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
    )


class ModelClient:
    """Thin typed wrapper around a LangChain chat model."""

    def __init__(self, chat_model: BaseChatModel, config: ModelConfig) -> None:
        self._chat_model = chat_model
        self.config = config

    def __repr__(self) -> str:
        return f"ModelClient(model_id={self.config.model_id!r})"

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool | dict | type[BaseModel]] = (),
    ) -> ModelResponse:
        started = time.perf_counter()
        try:
            runnable = self._chat_model.bind_tools(list(tools)) if tools else self._chat_model
            result = runnable.invoke(list(messages))
        except Exception as exc:
            raise _map_exception(exc, self.config.model_id) from None
        latency_ms = (time.perf_counter() - started) * 1000
        if not isinstance(result, AIMessage):
            raise ModelResponseError("Model returned a non-message response.")
        if result.invalid_tool_calls:
            names = ", ".join(str(call.get("name")) for call in result.invalid_tool_calls)
            raise ModelResponseError(f"Model produced an invalid tool call: {names}.")
        text = _text_of(result)
        calls = tuple(
            ToolCallRequest(name=c["name"], args=dict(c["args"]), id=str(c.get("id") or ""))
            for c in result.tool_calls
        )
        if not text.strip() and not calls:
            raise ModelResponseError("Model returned an empty response.")
        meta = result.usage_metadata or {}
        usage = TokenUsage(
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            total_tokens=meta.get("total_tokens", 0),
        )
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            model_id=self.config.model_id,
            latency_ms=latency_ms,
        )


def create_openai_client(config: ModelConfig, api_key: SecretStr) -> ModelClient:
    """Build a ChatOpenAI-backed client. No network call happens here."""
    from langchain_openai import ChatOpenAI

    extra: dict[str, Any] = {}
    if config.temperature is not None:
        extra["temperature"] = config.temperature
    if config.reasoning_effort is not None:
        extra["reasoning_effort"] = config.reasoning_effort
    chat = ChatOpenAI(
        model=config.model_id,
        timeout=config.timeout_s,
        max_retries=config.max_retries,
        api_key=api_key,
        use_responses_api=config.use_responses_api,
        **extra,
    )
    return ModelClient(chat, config)
