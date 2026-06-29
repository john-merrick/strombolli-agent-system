"""Static configuration: recursion budgets, thresholds, and model-per-surface.

These are the knobs PRD §5 says live in config (not hardcoded in nodes):

* the two recursion bounds (:data:`Budgets.max_inner_turns`, the Agent SDK
  agent-loop cap; :data:`Budgets.max_outer_revisions`, the verifier revise-edge
  cap) and the hard cost ceiling (:data:`Budgets.max_tokens_per_task`); and
* the **two model surfaces** (PRD §4): the Claude coder (Agent SDK), the cheap
  reasoning model (spec/router/memory via the LiteLLM gateway), and the
  *non-Claude* verifier (Gemini 2.5 Pro — pinned, PRD §11.1) so its judgment is
  independent of the coder.

Values are sourced from :class:`~stromboli.settings.Settings` (env-backed) via
:func:`from_settings`; the dataclasses keep the rest of the system from reaching
into ``Settings`` directly and give tests a trivial way to construct budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from stromboli.settings import Settings

#: How the coder (Agent SDK) authenticates (PRD §4a):
#: * ``subscription`` — run on the logged-in ``claude`` plan tokens; the process
#:   must have **no** ``ANTHROPIC_API_KEY`` (a stray one silently flips to
#:   pay-as-you-go). This is the default.
#: * ``api_key`` — a Platform API key for predictable per-token billing.
AuthMode = Literal["subscription", "api_key"]
DEFAULT_AUTH_MODE: AuthMode = "subscription"

#: The default coder model — Claude, reached through the Agent SDK (PRD §4).
DEFAULT_CODER_MODEL = "claude-opus-4-8"
#: The default cheap reasoning model for spec/router/memory (Haiku-class, PRD §4).
DEFAULT_REASONING_MODEL = "claude-haiku-4-5-20251001"
#: The default lightweight model for the prompt agent (Spec → coding prompt).
DEFAULT_PROMPT_MODEL = "gemini-3.5-flash"
#: The default verifier model — deliberately *non-Claude* (PRD §11.1, resolved).
DEFAULT_VERIFIER_MODEL = "gemini/gemini-2.5-pro"


@dataclass(frozen=True)
class Budgets:
    """The recursion + cost bounds for a single task (PRD §5).

    Note (PRD §4a): under ``subscription`` auth the binding constraint is the
    plan's **rate-limit window**, not dollars — a rate-limit cutoff is handled as
    a retryable escalation (pause + resume the SDK session), so
    ``max_tokens_per_task`` is a backstop ceiling, not the primary control.
    """

    #: Cap on the Agent SDK agent-loop turns per coding attempt (inner recursion).
    max_inner_turns: int = 25
    #: Cap on verifier revise edges before escalate (outer recursion).
    max_outer_revisions: int = 3
    #: Backstop token ceiling across both model surfaces, per task.
    max_tokens_per_task: int = 2_000_000

    def __post_init__(self) -> None:
        if self.max_inner_turns < 1:
            raise ValueError("max_inner_turns must be >= 1")
        if self.max_outer_revisions < 0:
            raise ValueError("max_outer_revisions must be >= 0")
        if self.max_tokens_per_task < 1:
            raise ValueError("max_tokens_per_task must be >= 1")


@dataclass(frozen=True)
class Models:
    """The model name per surface (PRD §4: two surfaces, three roles)."""

    #: The coder — Claude, via the Agent SDK (Platform API key).
    coder: str = DEFAULT_CODER_MODEL
    #: Spec / router / memory — cheap, via the LiteLLM gateway.
    reasoning: str = DEFAULT_REASONING_MODEL
    #: The prompt agent (Spec → coding prompt) — lightweight, via the gateway.
    prompt: str = DEFAULT_PROMPT_MODEL
    #: The verifier — non-Claude, via the LiteLLM gateway (independent judgment).
    verifier: str = DEFAULT_VERIFIER_MODEL


@dataclass(frozen=True)
class Config:
    """The assembled static configuration for a run."""

    budgets: Budgets
    models: Models
    #: How the coder authenticates (PRD §4a).
    auth_mode: AuthMode = DEFAULT_AUTH_MODE


def from_settings(settings: Settings) -> Config:
    """Assemble :class:`Config` from the env-backed :class:`Settings`."""
    return Config(
        budgets=Budgets(
            max_inner_turns=settings.max_inner_turns,
            max_outer_revisions=settings.max_outer_revisions,
            max_tokens_per_task=settings.max_tokens_per_task,
        ),
        models=Models(
            coder=settings.coder_model,
            reasoning=settings.reasoning_model,
            prompt=settings.prompt_model,
            verifier=settings.verifier_model,
        ),
        auth_mode=settings.coder_auth_mode,
    )


__all__ = [
    "DEFAULT_AUTH_MODE",
    "DEFAULT_CODER_MODEL",
    "DEFAULT_PROMPT_MODEL",
    "DEFAULT_REASONING_MODEL",
    "DEFAULT_VERIFIER_MODEL",
    "AuthMode",
    "Budgets",
    "Config",
    "Models",
    "from_settings",
]
