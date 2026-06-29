"""Application settings loaded from the environment / a local ``.env`` file.

All configuration is sourced from environment variables (optionally backed by a
``.env`` file in the project root). Every variable in :data:`REQUIRED_ENV_VARS`
is mandatory; if any is missing, :func:`load_settings` fails fast with a
:class:`MissingSettingsError` that names the offending key(s).

The two model surfaces (PRD §4) authenticate differently:

* the **coder** (Claude Agent SDK) authenticates per ``CODER_AUTH_MODE`` (PRD
  §4a): ``subscription`` (default) runs on the logged-in ``claude`` plan tokens
  with **no** API key, while ``api_key`` uses a Platform key
  (:attr:`Settings.anthropic_api_key`) for per-token billing; and
* the **reasoning + verifier** calls go through the **LiteLLM gateway**
  (:attr:`Settings.litellm_base_url` + :attr:`Settings.litellm_api_key`), which
  maps model names to providers — so the non-Claude verifier needs no separate
  provider key here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from stromboli.config import (
    DEFAULT_AUTH_MODE,
    DEFAULT_CODER_MODEL,
    DEFAULT_REASONING_MODEL,
    DEFAULT_VERIFIER_MODEL,
    AuthMode,
)

#: The environment variables Stromboli requires to run, in declaration order.
#: ``ANTHROPIC_API_KEY`` is intentionally *not* here — it is required only under
#: ``CODER_AUTH_MODE=api_key`` and must be **absent** under ``subscription``
#: (enforced in :meth:`Settings.check_coder_auth`, PRD §4a).
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "NOTION_TOKEN",
    "NOTION_TASK_DB_ID",
    "GITHUB_TOKEN",
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

    # -- Coder surface: Claude Agent SDK (PRD §4 / §4a) --------------------- #
    #: ``subscription`` (default) runs on the logged-in ``claude`` plan tokens
    #: and must have NO api key; ``api_key`` uses the Platform key below.
    coder_auth_mode: AuthMode = Field(
        default=DEFAULT_AUTH_MODE, alias="CODER_AUTH_MODE"
    )
    #: Platform API key — only set (and required) under ``api_key`` auth mode.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
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

    @field_validator(
        "workspace_root", "chroma_persist_dir", "checkpoint_db_path", mode="after"
    )
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        """Expand a leading ``~`` so ``~/foo`` resolves to the home directory."""
        return value.expanduser()

    @model_validator(mode="after")
    def check_coder_auth(self) -> Settings:
        """Fail closed on the coder auth mode (PRD §4a).

        Under ``subscription`` a present ``ANTHROPIC_API_KEY`` (in ``.env`` *or*
        the process env — pydantic-settings reads both) would silently flip the
        Agent SDK onto pay-as-you-go, so we reject it outright. Under ``api_key``
        the key is mandatory.
        """
        if self.coder_auth_mode == "subscription" and self.anthropic_api_key:
            raise ValueError(
                "CODER_AUTH_MODE=subscription requires ANTHROPIC_API_KEY to be "
                "UNSET (a stray key silently switches the coder to pay-as-you-go). "
                "Unset it, or set CODER_AUTH_MODE=api_key to bill per token."
            )
        if self.coder_auth_mode == "api_key" and not self.anthropic_api_key:
            raise ValueError(
                "CODER_AUTH_MODE=api_key requires ANTHROPIC_API_KEY to be set."
            )
        return self


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
