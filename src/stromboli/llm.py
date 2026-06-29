"""Route the build agent (``claude -p``) through a LiteLLM proxy, pinned to Opus.

The worker spawns the Claude Code CLI as a subprocess; the CLI picks its endpoint
and model up from the environment. So instead of a direct ``ANTHROPIC_API_KEY``,
we map the configured LiteLLM proxy onto the CLI's gateway env vars and install
them on the process environment, which the spawned ``claude`` inherits:

* ``ANTHROPIC_BASE_URL``        ← LiteLLM proxy base URL
* ``ANTHROPIC_AUTH_TOKEN``      ← LiteLLM virtual key (sent as a bearer token)
* ``ANTHROPIC_MODEL``           ← the model (Opus) for the main loop
* ``ANTHROPIC_SMALL_FAST_MODEL``← same model, so even background calls hit Opus

``ANTHROPIC_API_KEY`` is removed so the CLI authenticates against the proxy with
the auth token rather than trying the Anthropic API directly.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

from stromboli.settings import Settings


def claude_gateway_env(*, base_url: str, api_key: str, model: str) -> dict[str, str]:
    """The Claude Code gateway env vars for a LiteLLM proxy pinned to ``model``."""
    return {
        # Strip any trailing slash so the CLI forms ``…/v1/messages`` cleanly.
        "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_MODEL": model,
        # Pin the small/fast (background) model to the same model so every call,
        # including Claude Code's housekeeping requests, routes to Opus.
        "ANTHROPIC_SMALL_FAST_MODEL": model,
    }


def apply_claude_gateway_env(
    settings: Settings, environ: MutableMapping[str, str] | None = None
) -> None:
    """Install the LiteLLM→Claude gateway env on the process (default ``os.environ``).

    Spawned ``claude`` subprocesses inherit it. ``ANTHROPIC_API_KEY`` is dropped
    so the proxy auth token is used instead of direct Anthropic auth.
    """
    env = environ if environ is not None else os.environ
    env.update(
        claude_gateway_env(
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
            model=settings.litellm_model,
        )
    )
    env.pop("ANTHROPIC_API_KEY", None)


__all__ = [
    "apply_claude_gateway_env",
    "claude_gateway_env",
]
