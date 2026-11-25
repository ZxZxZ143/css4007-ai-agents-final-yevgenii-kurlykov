import logging
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from deskpilot import limits
from deskpilot.defaults import DEFAULT_MODEL_ID
from deskpilot.settings import (
    Budgets,
    Settings,
    SettingsError,
    default_settings_path,
    load_settings,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "settings.example.toml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file(tmp_path: Path) -> None:
    loaded = load_settings(tmp_path / "missing.toml")
    assert loaded.source is None
    assert loaded.warnings == ()
    s = loaded.settings
    assert s.model_id == DEFAULT_MODEL_ID
    assert s.language == "ru"
    assert s.approvals.modify == "text"
    assert s.approvals.dangerous == "text"
    assert s.budgets.retries_per_operation == 2
    for name, maximum in limits.HARD_MAX.items():
        assert 0 < getattr(s.budgets, name) <= maximum
    assert all(r.is_absolute() for r in s.allowed_roots)
    assert {r.name for r in s.allowed_roots} >= {"Desktop", "Documents", "Downloads"}


def test_default_roots_follow_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Settings().allowed_roots[0].parent == tmp_path


def test_default_path_computed_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_settings_path() == tmp_path / "DeskPilot" / "settings.toml"


def test_load_valid_file(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    path = write(
        tmp_path,
        f'language = "en"\nallowed_roots = [{str(root)!r}]\n[approvals]\nmodify = "none"\n'
        "[budgets]\nsteps = 10\n",
    )
    loaded = load_settings(path)
    assert loaded.source == path
    assert loaded.settings.language == "en"
    assert loaded.settings.allowed_roots == (root,)
    assert loaded.settings.approvals.modify == "none"
    assert loaded.settings.budgets.steps == 10


def test_bad_toml_reports_path_and_position(tmp_path: Path) -> None:
    path = write(tmp_path, 'language = "en"\nsteps = = 3\n')
    with pytest.raises(SettingsError) as exc:
        load_settings(path)
    message = str(exc.value)
    assert str(path) in message
    assert "line 2" in message


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("language = 5\n", "language"),
        ('language = "de"\n', "language"),
        ('model_id = ""\n', "model_id"),
        ("surprise = 1\n", "surprise"),
        ("[budgets]\nsurprise = 1\n", "budgets.surprise"),
        ('allowed_roots = ["relative/dir"]\n', "absolute"),
        ("allowed_roots = ['C:\\']\n", "drive roots"),
        ("allowed_roots = ['\\\\server\\share\\x']\n", "UNC"),
        ("allowed_roots = ['C:\\..']\n", "'..'"),
        ("allowed_roots = ['C:\\Users\\..']\n", "'..'"),
        ("allowed_roots = ['C:/x/../..']\n", "'..'"),
        ("allowed_roots = ['\\\\?\\C:\\x']\n", "device paths"),
        ("allowed_roots = ['\\\\.\\C:\\x']\n", "device paths"),
        ("sandbox_dir = 'rel'\n", "absolute"),
        ("sandbox_dir = 'C:\\x\\..\\y'\n", "'..'"),
        ("sandbox_dir = 'C:\\'\n", "drive roots"),
        ("sandbox_dir = '\\\\?\\C:\\x'\n", "device paths"),
        ("[budgets]\nretries_per_operation = -1\n", "budgets.retries_per_operation"),
        ('[approvals]\ndangerous = "none"\n', "always require approval"),
        ('[approvals]\nmodify = "maybe"\n', "approvals.modify"),
        ("[budgets]\nsteps = 0\n", "budgets.steps"),
        ("[budgets]\nsteps = -3\n", "budgets.steps"),
        ('[budgets]\nsteps = "ten"\n', "budgets.steps"),
        ("[budgets]\nsteps = 2.5\n", "budgets.steps"),
        ("[budgets]\nsteps = true\n", "budgets.steps"),
        ('browser = "own"\n', "browser"),
    ],
)
def test_invalid_settings_rejected(tmp_path: Path, text: str, needle: str) -> None:
    with pytest.raises(SettingsError) as exc:
        load_settings(write(tmp_path, text))
    assert needle in str(exc.value)


def test_multiple_errors_one_per_line(tmp_path: Path) -> None:
    path = write(tmp_path, 'language = "de"\n[budgets]\nsteps = 0\n')
    with pytest.raises(SettingsError) as exc:
        load_settings(path)
    lines = str(exc.value).splitlines()
    assert any(line.startswith("language:") for line in lines)
    assert any(line.startswith("budgets.steps:") for line in lines)


def test_values_above_hard_max_are_clamped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write(tmp_path, "[budgets]\nsteps = 100000\ntokens = 99999999\nbrowser_tabs = 3\n")
    with caplog.at_level(logging.WARNING, logger="deskpilot.settings"):
        loaded = load_settings(path)
    assert loaded.settings.budgets.steps == limits.HARD_MAX["steps"]
    assert loaded.settings.budgets.tokens == limits.HARD_MAX["tokens"]
    assert loaded.settings.budgets.browser_tabs == 3
    by_field = {w.field: w for w in loaded.warnings}
    assert set(by_field) == {"budgets.steps", "budgets.tokens"}
    assert by_field["budgets.steps"].given == 100000
    assert by_field["budgets.steps"].clamped_to == limits.HARD_MAX["steps"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_value_equal_to_max_is_not_warned() -> None:
    s = Settings.from_mapping({"budgets": {"steps": limits.HARD_MAX["steps"]}})
    assert s.budgets.steps == limits.HARD_MAX["steps"]


@pytest.mark.parametrize(
    "text",
    [
        "hard_deny = []\n",
        "denylist = ['C:/x']\n",
        "forbidden_commands = ['format']\n",
        "[budgets]\nhard_limits = 1\n",
        "[approvals]\nforbidden = 'text'\n",
        "Hard-Deny = []\n",
        "[[x]]\ndeny = 1\n",
        "[budgets]\nforbidden-commands = 1\n",
    ],
)
def test_ceiling_keys_rejected_explicitly(tmp_path: Path, text: str) -> None:
    with pytest.raises(SettingsError) as exc:
        load_settings(write(tmp_path, text))
    assert "code constants and cannot be configured" in str(exc.value)


def test_limits_are_immutable() -> None:
    assert isinstance(limits.HARD_MAX, MappingProxyType)
    with pytest.raises(TypeError):
        limits.HARD_MAX["steps"] = 10**9  # type: ignore[index]
    assert isinstance(limits.HARD_DENY_DIR_TEMPLATES, tuple)
    assert isinstance(limits.HARD_DENY_FILE_PATTERNS, tuple)
    assert isinstance(limits.ALWAYS_DENIED_RISK_CLASSES, frozenset)
    assert isinstance(limits.ALWAYS_APPROVED_RISK_CLASSES, frozenset)
    assert "%WINDIR%" in limits.HARD_DENY_DIR_TEMPLATES
    assert "*.pem" in limits.HARD_DENY_FILE_PATTERNS


def test_settings_have_no_ceiling_fields() -> None:
    names = set(Settings.model_fields) | set(Budgets.model_fields)
    assert not names & {"hard_deny", "denylist", "forbidden", "forbidden_commands"}
    assert set(Budgets.model_fields) == set(limits.HARD_MAX)


def test_settings_are_frozen() -> None:
    s = Settings()
    with pytest.raises(ValidationError):
        s.language = "en"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        s.budgets.steps = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        s.approvals.modify = "none"  # type: ignore[misc]


def test_example_toml_is_valid() -> None:
    loaded = load_settings(EXAMPLE)
    assert loaded.warnings == ()
    defaults = Settings()
    assert loaded.settings.budgets == defaults.budgets
    assert loaded.settings.approvals == defaults.approvals
    assert loaded.settings.model_id == DEFAULT_MODEL_ID


def test_zero_retries_allowed_other_zero_budgets_rejected() -> None:
    assert Settings.from_mapping({"budgets": {"retries_per_operation": 0}})
    with pytest.raises(SettingsError):
        Settings.from_mapping({"budgets": {"tool_calls": 0}})


def test_credential_templates_present() -> None:
    for entry in ("~/.aws", "~/.kube", "%APPDATA%/Microsoft/Protect"):
        assert entry in limits.HARD_DENY_DIR_TEMPLATES
    assert limits.HARD_DENY_INSTALL_DIR is True


def test_tier_and_cache_defaults() -> None:
    s = Settings()
    assert s.strong_model_id == "gpt-5.5"
    assert s.budgets.strong_model_calls == 5
    assert (s.cache.enabled, s.cache.ttl_days, s.cache.max_entries) == (True, 14, 200)


def test_strong_calls_clamped_to_hard_max() -> None:
    assert limits.HARD_MAX["strong_model_calls"] == 20
    loaded = Settings.from_mapping({"budgets": {"strong_model_calls": 500}})
    assert loaded.budgets.strong_model_calls == 20


@pytest.mark.parametrize(
    "data",
    [
        {"strong_model_id": ""},
        {"budgets": {"strong_model_calls": 0}},
        {"cache": {"ttl_days": 0}},
        {"cache": {"ttl_days": 91}},
        {"cache": {"ttl_days": 1.5}},
        {"cache": {"max_entries": 1001}},
        {"cache": {"max_entries": 0}},
        {"cache": {"unknown": 1}},
    ],
)
def test_invalid_tier_and_cache_values(data: dict) -> None:
    with pytest.raises(SettingsError):
        Settings.from_mapping(data)
