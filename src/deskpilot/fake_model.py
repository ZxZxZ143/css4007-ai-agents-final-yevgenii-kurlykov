"""Scripted chat model for offline tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from deskpilot.model import ModelResponseError


def _usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def ai_text(text: str, *, input_tokens: int = 1, output_tokens: int = 1) -> AIMessage:
    return AIMessage(content=text, usage_metadata=_usage(input_tokens, output_tokens))


def ai_tool_call(
    name: str,
    args: dict[str, Any],
    id: str | None = None,
    *,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": id or f"call_{name}", "type": "tool_call"}],
        usage_metadata=_usage(input_tokens, output_tokens),
    )


class ScriptedChatModel(BaseChatModel):
    """Returns scripted AIMessages in order; an Exception step is raised instead."""

    script: list[Any] = Field(default_factory=list)
    received: list[list[BaseMessage]] = Field(default_factory=list)
    bind_calls: list[dict[str, Any]] = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "deskpilot-scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        names: list[str] = []
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name") or tool.get("function", {}).get("name", "")
            else:
                name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
            names.append(str(name))
        self.bind_calls.append({"tools": names, "kwargs": kwargs})
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        if self.cursor >= len(self.script):
            raise ModelResponseError("Scripted model exhausted: no more scripted responses.")
        step = self.script[self.cursor]
        self.cursor += 1
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])
