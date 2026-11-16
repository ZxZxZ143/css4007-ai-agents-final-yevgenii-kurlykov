import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_MODEL = "gpt-5.6-luna"


class ConfigurationError(Exception):
    """An actionable local configuration error."""


def create_model() -> ChatOpenAI:
    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError("Set OPENAI_API_KEY in the environment or in your local .env file.")

    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL,
        api_key=SecretStr(api_key),
        base_url="https://api.openai.com/v1",
        use_responses_api=True,
        reasoning={"effort": "low"},
        store=False,
        include=["reasoning.encrypted_content"],
        timeout=25,
        max_retries=0,
        max_tokens=1024,
    )
