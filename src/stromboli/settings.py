"""Application settings loaded from the environment / a local ``.env`` file.

All configuration is sourced from environment variables (optionally backed by a
``.env`` file in the project root). Every variable in :data:`REQUIRED_ENV_VARS`
is mandatory; if any is missing, :func:`load_settings` fails fast with a
:class:`MissingSettingsError` that names the offending key(s).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The environment variables Stromboli requires to run, in declaration order.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "NOTION_TOKEN",
    "GITHUB_TOKEN",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "TUNNEL_PUBLIC_URL",
    "WORKSPACE_ROOT",
    "ANTHROPIC_API_KEY",
    "DISPATCH_SHARED_SECRET",
)


class MissingSettingsError(RuntimeError):
    """Raised when one or more required environment variables are unset."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        joined = ", ".join(missing)
        super().__init__(
            f"Missing required environment variable(s): {joined}. "
            "Set them in the environment or in a .env file (see .env.example)."
        )


class Settings(BaseSettings):
    """Typed, validated runtime configuration for the Stromboli worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    notion_token: str = Field(alias="NOTION_TOKEN")
    github_token: str = Field(alias="GITHUB_TOKEN")
    langfuse_public_key: str = Field(alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(alias="LANGFUSE_HOST")
    tunnel_public_url: str = Field(alias="TUNNEL_PUBLIC_URL")
    workspace_root: Path = Field(alias="WORKSPACE_ROOT")
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    dispatch_shared_secret: str = Field(alias="DISPATCH_SHARED_SECRET")

    #: Which build engine to use. Optional; defaults to the legacy Ralph loop so
    #: existing behaviour is unchanged until ``graph`` is deliberately selected.
    stromboli_engine: Literal["ralph", "graph"] = Field(
        default="ralph", alias="STROMBOLI_ENGINE"
    )

    #: Telegram alert bot token + target chat id (optional). When both are set,
    #: build-lifecycle notifications are pushed to the chat. Resolve from a secret
    #: manager (e.g. 1Password ``op inject``/``op run``) at deploy time.
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")


def load_settings(**overrides: Any) -> Settings:
    """Construct :class:`Settings`, failing fast on missing required variables.

    Any keyword arguments are forwarded to the :class:`Settings` constructor;
    this is primarily useful in tests (e.g. ``load_settings(_env_file=None)`` to
    bypass ``.env`` discovery). On a ``missing``-type validation error the env
    var names are collected and surfaced via :class:`MissingSettingsError`.
    """

    try:
        return Settings(**overrides)
    except ValidationError as exc:
        missing = [
            str(error["loc"][0]).upper()
            for error in exc.errors()
            if error["type"] == "missing" and error["loc"]
        ]
        if missing:
            # Preserve declaration order for a stable, readable message.
            ordered = [key for key in REQUIRED_ENV_VARS if key in set(missing)]
            raise MissingSettingsError(ordered) from exc
        raise
