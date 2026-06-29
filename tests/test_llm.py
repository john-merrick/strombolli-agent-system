"""Tests for the LiteLLM → Claude Code gateway env mapping."""

from __future__ import annotations

from stromboli.llm import apply_claude_gateway_env, claude_gateway_env
from stromboli.settings import Settings, load_settings

_ENV = {
    "NOTION_TOKEN": "n",
    "GITHUB_TOKEN": "g",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "LANGFUSE_HOST": "https://lf",
    "TUNNEL_PUBLIC_URL": "https://t",
    "WORKSPACE_ROOT": "/tmp/ws",
    "LITELLM_BASE_URL": "https://litellm.example.com",
    "LITELLM_API_KEY": "sk-litellm-xyz",
    "DISPATCH_SHARED_SECRET": "s",
}


def _settings(**overrides: str) -> Settings:
    return load_settings(_env_file=None, **{**_ENV, **overrides})


def test_gateway_env_maps_proxy_and_pins_model() -> None:
    env = claude_gateway_env(
        base_url="https://litellm.example.com", api_key="sk-key", model="claude-opus-4-8"
    )
    assert env == {
        "ANTHROPIC_BASE_URL": "https://litellm.example.com",
        "ANTHROPIC_AUTH_TOKEN": "sk-key",
        "ANTHROPIC_MODEL": "claude-opus-4-8",
        # Background/small-fast calls also route to Opus.
        "ANTHROPIC_SMALL_FAST_MODEL": "claude-opus-4-8",
    }


def test_apply_installs_gateway_and_drops_anthropic_key() -> None:
    env: dict[str, str] = {"ANTHROPIC_API_KEY": "sk-ant-should-be-removed"}
    apply_claude_gateway_env(_settings(), environ=env)

    assert env["ANTHROPIC_BASE_URL"] == "https://litellm.example.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-litellm-xyz"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-opus-4-8"
    # The direct Anthropic key must be gone so the proxy auth is used.
    assert "ANTHROPIC_API_KEY" not in env


def test_base_url_trailing_slash_is_stripped() -> None:
    env = claude_gateway_env(
        base_url="http://localhost:4000/", api_key="k", model="claude-opus"
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"


def test_litellm_model_override_is_respected() -> None:
    apply = _settings(LITELLM_MODEL="claude-opus-4-8-custom-alias")
    env: dict[str, str] = {}
    apply_claude_gateway_env(apply, environ=env)
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8-custom-alias"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-opus-4-8-custom-alias"
