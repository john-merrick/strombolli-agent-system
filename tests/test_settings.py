"""Tests for :mod:`stromboli.settings`."""

from __future__ import annotations

from pathlib import Path

import pytest

from stromboli.settings import (
    REQUIRED_ENV_VARS,
    MissingSettingsError,
    Settings,
    load_settings,
)

VALID_ENV = {
    "NOTION_TOKEN": "secret_notion",
    "GITHUB_TOKEN": "ghp_token",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-123",
    "LANGFUSE_SECRET_KEY": "sk-lf-456",
    "LANGFUSE_HOST": "https://cloud.langfuse.com",
    "TUNNEL_PUBLIC_URL": "https://tunnel.example.com",
    "WORKSPACE_ROOT": "/tmp/stromboli-workspaces",
    "ANTHROPIC_API_KEY": "sk-ant-789",
    "DISPATCH_SHARED_SECRET": "shared-secret",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no required var leaks in from the real environment."""
    for key in REQUIRED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_loads_all_values_with_correct_types(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(monkeypatch, VALID_ENV)

    settings = load_settings(_env_file=None)

    assert isinstance(settings, Settings)
    assert settings.notion_token == "secret_notion"
    assert settings.github_token == "ghp_token"
    assert settings.langfuse_public_key == "pk-lf-123"
    assert settings.langfuse_secret_key == "sk-lf-456"
    assert settings.langfuse_host == "https://cloud.langfuse.com"
    assert settings.tunnel_public_url == "https://tunnel.example.com"
    assert settings.anthropic_api_key == "sk-ant-789"
    assert settings.dispatch_shared_secret == "shared-secret"
    # WORKSPACE_ROOT is coerced to a Path.
    assert settings.workspace_root == Path("/tmp/stromboli-workspaces")
    assert isinstance(settings.workspace_root, Path)


@pytest.mark.parametrize("missing_key", REQUIRED_ENV_VARS)
def test_missing_required_var_fails_fast_naming_the_key(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, missing_key: str
) -> None:
    env = {k: v for k, v in VALID_ENV.items() if k != missing_key}
    _set(monkeypatch, env)

    with pytest.raises(MissingSettingsError) as excinfo:
        load_settings(_env_file=None)

    assert missing_key in excinfo.value.missing
    assert missing_key in str(excinfo.value)


def test_multiple_missing_vars_all_named(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        k: v
        for k, v in VALID_ENV.items()
        if k not in {"NOTION_TOKEN", "ANTHROPIC_API_KEY"}
    }
    _set(monkeypatch, env)

    with pytest.raises(MissingSettingsError) as excinfo:
        load_settings(_env_file=None)

    assert set(excinfo.value.missing) == {"NOTION_TOKEN", "ANTHROPIC_API_KEY"}


def test_reads_values_from_env_file(
    clean_env: None, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in VALID_ENV.items()) + "\n",
        encoding="utf-8",
    )

    settings = load_settings(_env_file=str(env_file))

    assert settings.notion_token == "secret_notion"
    assert settings.dispatch_shared_secret == "shared-secret"
