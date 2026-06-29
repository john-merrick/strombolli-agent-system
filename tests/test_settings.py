"""Tests for env-backed settings loading and fail-fast on missing vars."""

from __future__ import annotations

import pytest

from stromboli.settings import MissingSettingsError, load_settings

_FULL_ENV = {
    "NOTION_TOKEN": "n",
    "NOTION_TASK_DB_ID": "db",
    "GITHUB_TOKEN": "g",
    "ANTHROPIC_API_KEY": "a",
    "LITELLM_BASE_URL": "http://proxy",
    "LITELLM_API_KEY": "k",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "LANGFUSE_HOST": "http://lf",
    "WORKSPACE_ROOT": "/tmp/ws",
}


def test_load_settings_with_full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear optional vars that may leak in from the host env, for a hermetic test.
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(_env_file=None, **_FULL_ENV)
    assert settings.notion_task_db_id == "db"
    assert settings.anthropic_api_key == "a"
    assert settings.telegram_bot_token is None
    # Optional budgets fall back to defaults.
    assert settings.max_inner_turns == 25


def test_missing_required_vars_named(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear the env so nothing leaks in; then provide only a subset.
    for key in _FULL_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(MissingSettingsError) as excinfo:
        load_settings(_env_file=None, NOTION_TOKEN="n")
    missing = excinfo.value.missing
    assert "ANTHROPIC_API_KEY" in missing
    assert "NOTION_TOKEN" not in missing  # it was provided


def test_dispatch_server_vars_are_gone() -> None:
    settings = load_settings(_env_file=None, **_FULL_ENV)
    # The v0.1 dispatch server is removed — these attrs must not exist.
    assert not hasattr(settings, "dispatch_shared_secret")
    assert not hasattr(settings, "tunnel_public_url")
    assert not hasattr(settings, "stromboli_engine")
