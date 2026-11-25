"""Live model check (manual). Run with: uv run pytest -m live tests/test_model_live.py"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from deskpilot.model import ModelConfig, ModelConfigError, create_openai_client, load_api_key

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY") and not (ROOT / ".env").exists(),
        reason="OPENAI_API_KEY not available",
    ),
]


@tool
def get_current_time(timezone: str) -> str:
    """Return the current time in an IANA timezone, e.g. Asia/Tashkent."""
    return timezone


def test_live_tool_call() -> None:
    try:
        api_key = load_api_key(ROOT / ".env")
    except ModelConfigError:
        pytest.skip("OPENAI_API_KEY not set in environment or .env")
    config = ModelConfig(model_id=os.environ.get("DESKPILOT_MODEL") or ModelConfig().model_id)
    client = create_openai_client(config, api_key)
    response = client.invoke(
        [HumanMessage(content="What time is it in Asia/Tashkent? Use the tool.")],
        tools=[get_current_time],
    )
    assert response.tool_calls
    call = response.tool_calls[0]
    assert call.name == "get_current_time"
    evidence = {
        "model_id": response.model_id,
        "tool_call": {"name": call.name, "args": call.args},
        "usage": response.usage.model_dump(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    out = ROOT / ".private" / "evidence" / "f04-live-tool-call.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
