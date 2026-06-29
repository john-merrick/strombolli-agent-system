"""Application settings loaded from the environment / a local ``.env`` file.

All configuration is sourced from environment variables (optionally backed by a
``.env`` file in the project root). Every variable in :data:`REQUIRED_ENV_VARS`
is mandatory; if any is missing, :func:`load_settings` fails fast with a
:class:`MissingSettingsError` that names the offending key(s).

The two model surfaces (PRD §4) authenticate differently:

* the **coder** (Claude Agent SDK) uses a **Platform API key**
  (:attr:`Settings.anthropic_api_key`) for predictable per-token cost; and
* the **reasoning + verifier** calls go through the **LiteLLM gateway**
  (:attr:`Settings.litellm_base_url` + :attr:`Settings.litellm_api_key`), which
  maps model names to providers — so the non-Claude verifier needs no separate
  provider key here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from stromboli.config import (
    DEFAULT_CODER_MODEL,
    DEFAULT_REASONING_MODEL,
    DEFAULT_VERIFIER_MODEL,
)

#: The environment variables Stromboli requires to run, in declaration order.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "NOTION_TOKEN",
    "NOTION_TASK_DB_ID",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "WORKSPACE_ROOT",
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
    """Typed, validated runtime configuration for the Stromboli graph runtime."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Intake (Notion is the front-end for adding tasks) ------------------ #
    notion_token: str = Field(alias="NOTION_TOKEN")
    #: The Notion database the Intake source reads "Ready" tasks from.
    notion_task_db_id: str = Field(alias="NOTION_TASK_DB_ID")

    # -- GitHub (PR / Commit node) ------------------------------------------ #
    github_token: str = Field(alias="GITHUB_TOKEN")

    # -- Coder surface: Claude Agent SDK, Platform API key (PRD §4) --------- #
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    coder_model: str = Field(default=DEFAULT_CODER_MODEL, alias="CODER_MODEL")

    # -- Reasoning + verifier surface: LiteLLM gateway (PRD §4) ------------- #
    litellm_base_url: str = Field(alias="LITELLM_BASE_URL")
    litellm_api_key: str = Field(alias="LITELLM_API_KEY")
    reasoning_model: str = Field(
        default=DEFAULT_REASONING_MODEL, alias="REASONING_MODEL"
    )
    #: The non-Claude verifier model (PRD §11.1, pinned to Gemini 2.5 Pro).
    verifier_model: str = Field(default=DEFAULT_VERIFIER_MODEL, alias="VERIFIER_MODEL")

    # -- Observability ------------------------------------------------------ #
    langfuse_public_key: str = Field(alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(alias="LANGFUSE_HOST")

    # -- Filesystem --------------------------------------------------------- #
    #: Absolute path under which per-task git worktrees + sandbox state live.
    workspace_root: Path = Field(alias="WORKSPACE_ROOT")
    #: Where ChromaDB persists its three memory collections.
    chroma_persist_dir: Path = Field(
        default=Path(".stromboli/chroma"), alias="CHROMA_PERSIST_DIR"
    )
    #: The LangGraph checkpointer SQLite file (dev; Postgres deferred, §11.3).
    checkpoint_db_path: Path = Field(
        default=Path(".stromboli/checkpoints.db"), alias="CHECKPOINT_DB_PATH"
    )

    # -- Recursion + cost budgets (PRD §5) ---------------------------------- #
    max_inner_turns: int = Field(default=25, alias="MAX_INNER_TURNS")
    max_outer_revisions: int = Field(default=3, alias="MAX_OUTER_REVISIONS")
    max_tokens_per_task: int = Field(default=2_000_000, alias="MAX_TOKENS_PER_TASK")

    # -- Telegram notifications (optional) ---------------------------------- #
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


__all__ = ["REQUIRED_ENV_VARS", "MissingSettingsError", "Settings", "load_settings"]
