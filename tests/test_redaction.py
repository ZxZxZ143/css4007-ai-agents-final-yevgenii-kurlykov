import json

from deskpilot import redaction
from deskpilot.redaction import redact

OPENAI = "sk-proj-abcdefghijklmnop1234567890"
AWS = "AKIAABCDEFGHIJKLMNOP"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"


def test_key_based_redaction_nested():
    out = redact({"API-Key": "x1", "a": [{"Authorization": "b"}], "n": {"db_password": "p"}})
    assert out["API-Key"] == "[REDACTED]"
    assert out["a"][0]["Authorization"] == "[REDACTED]"
    assert out["n"]["db_password"] == "[REDACTED]"


def test_pattern_redaction():
    text = f"k={OPENAI} Bearer abcdef123456 {AWS} {PEM} url?password=hunter2&x=1"
    out = redact(text)
    for raw in (OPENAI, "abcdef123456", AWS, "MIIabc", "hunter2"):
        assert raw not in out
    assert "x=1" in out


def test_explicit_secrets_and_short_ignored():
    out = redact(["my-s3cret-value", "abc"], secrets=["s3cret", "abc", ""])
    assert out[0] == "my-[REDACTED]-value"
    assert out[1] == "abc"


def test_secret_split_by_truncation_does_not_leak():
    text = "a" * (redaction.MAX_STRING - 5) + OPENAI
    out = redact(text)
    assert "sk-" not in out
    assert "truncated" in out


def test_private_reasoning_omitted():
    out = redact({"reasoning": "think", "Chain-Of-Thought": "x", "scratchpad": 1, "ok": 1})
    assert out == {
        "reasoning": "[OMITTED]",
        "Chain-Of-Thought": "[OMITTED]",
        "scratchpad": "[OMITTED]",
        "ok": 1,
    }


def test_long_string_truncated():
    out = redact("x" * 5000)
    assert out.startswith("x" * 2000)
    assert "[truncated 3000 chars]" in out


def test_long_list_and_keys_bounded():
    out = redact(list(range(80)))
    assert len(out) == 51
    assert "30 more" in out[-1]
    d = redact({f"k{i}": i for i in range(130)})
    assert len(d) == 101
    assert d["_omitted_keys"] == 30


def test_depth_limit():
    value: object = "leaf"
    for _ in range(10):
        value = {"a": value}
    out = redact(value)
    for _ in range(redaction.MAX_DEPTH):
        out = out["a"]
    assert out == "[depth limit]"


def test_non_json_types_bounded_repr():
    class Weird:
        def __repr__(self) -> str:
            return "W" * 5000

    out = redact({"o": Weird(), "s": {1, 2}, "f": float("nan")})
    json.dumps(out)
    assert len(out["o"]) < 2100
    assert out["f"] == "nan"


def test_token_usage_counters_survive():
    usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15, "max_tokens": 100}
    assert redact({"usage": usage, "budgets": {"tokens": 5000}}) == {
        "usage": usage,
        "budgets": {"tokens": 5000},
    }


def test_token_strings_redacted():
    out = redact(
        {"access_token": "abc123xyz", "token": "abcd1234", "id_token": "q", "refresh_token": ["x"]}
    )
    assert set(out.values()) == {"[REDACTED]"}


def test_numeric_api_key_still_redacted():
    # Only the token family is exempt for numbers; other secret keys redact any type.
    assert redact({"api_key": 12345, "password": 1}) == {
        "api_key": "[REDACTED]",
        "password": "[REDACTED]",
    }


def test_private_reasoning_variants_omitted():
    keys = ["reasoning_content", "reasoning_summary", "Thinking", "thoughts", "agent-thought"]
    out = redact({k: "private" for k in keys})
    assert set(out.values()) == {"[OMITTED]"}


def test_token_mapping_rules():
    out = redact({"access_token": {"value": "abc123xyz"}, "token": ["abcd1234"]})
    assert out == {"access_token": "[REDACTED]", "token": "[REDACTED]"}
    usage = redact({"token_usage": {"input_tokens": 1, "api_key": "k"}, "tokens": {"a": 2}})
    assert usage == {
        "token_usage": {"input_tokens": 1, "api_key": "[REDACTED]"},
        "tokens": {"a": 2},
    }


def test_raising_repr_and_huge_input():
    class Bad:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    assert redact(Bad()) == "<unrepresentable Bad>"
    out = redact("z" * 5_000_000)
    assert len(out) < 2100
    assert "[truncated 4998000 chars]" in out


def test_colliding_keys_disambiguated():
    out = redact({"sk-proj-abcdefghijklmnop1": 1, "sk-proj-abcdefghijklmnop2": 2})
    assert sorted(out) == ["[REDACTED]", "[REDACTED]#2"]
    assert sorted(out.values()) == [1, 2]
