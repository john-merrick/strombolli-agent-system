"""The Claude Agent SDK coder — the inner write→test→fix loop (PRD §6.4).

We do **not** author the write-test-fix loop; the SDK *is* the loop. This wrapper
only does the three things PRD §6.4 lists:

1. **Bounds it** — caps agent-loop turns at ``max_turns`` (the inner recursion
   bound) and accepts an optional USD budget.
2. **Constrains it** — pre-approves exactly the allowlisted tools and **fails
   closed** on anything else via a ``can_use_tool`` callback, so an unattended
   run never hangs on a permission prompt.
3. **Captures it** — collects the final diff (an injected ``diff_fn``), the SDK
   ``session_id`` (so a revise pass can resume rather than start cold), per-turn
   tool/usage records (for tracing), and the terminal ``ResultMessage`` subtype.

The node's only oracle is the sandbox test run (the caller runs it after this);
the coder never judges its own intent. A non-clean SDK result subtype surfaces
as a :class:`CoderError`, never a silent pass.

``query_fn`` (the SDK ``query``) and ``diff_fn`` (``git diff``) are injected, so
the control flow is unit-tested without launching a real agent or git.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from claude_agent_sdk import (
    query as sdk_query,
)

from stromboli.config import DEFAULT_AUTH_MODE, AuthMode

#: The exact tools the coding job needs; everything else fails closed (§6.4).
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Glob", "Grep")

#: SDK ``ResultMessage`` subtypes that are a clean terminal (success or a bounded
#: budget exit) rather than an execution failure.
CLEAN_SUBTYPES: frozenset[str] = frozenset({"success", "error_max_turns"})

#: The async SDK query callable (``claude_agent_sdk.query``-shaped), injectable.
QueryFn = Callable[..., AsyncIterator[Any]]
#: Returns the worktree's diff against HEAD; injectable for tests.
DiffFn = Callable[[Path], str]


class CoderError(RuntimeError):
    """The SDK run ended in a non-clean state (an execution error, not budget)."""


class RateLimitError(RuntimeError):
    """The subscription rate-limit window was hit mid-run (PRD §4a).

    Not a failure — a *retryable escalation*: the caller pauses and resumes the
    captured ``session_id`` after ``resets_at``, rather than crashing the task.
    """

    def __init__(
        self, *, session_id: str | None, resets_at: str | None = None
    ) -> None:
        self.session_id = session_id
        self.resets_at = resets_at
        when = f" (resets at {resets_at})" if resets_at else ""
        super().__init__(f"Coder hit the rate-limit window{when}.")


class Coder(Protocol):
    """The coding seam the graph depends on (run a bounded agent loop)."""

    def run(
        self, prompt: str, cwd: str | Path, *, resume: str | None = None
    ) -> CoderRun: ...


@dataclass(frozen=True)
class TurnRecord:
    """One agent-loop turn: the tools it invoked and its usage (for tracing)."""

    index: int
    tools: tuple[str, ...]
    usage: dict[str, Any] | None


@dataclass(frozen=True)
class CoderRun:
    """The captured outcome of one bounded Agent SDK run."""

    diff: str
    final_text: str
    turns: int
    session_id: str | None
    subtype: str
    is_error: bool
    cost_usd: float | None
    usage: dict[str, Any] | None
    turn_records: tuple[TurnRecord, ...]

    @property
    def clean(self) -> bool:
        """Whether the run terminated cleanly (success or a bounded budget exit)."""
        return (not self.is_error) and self.subtype in CLEAN_SUBTYPES


def _git_diff(root: Path) -> str:
    """Default :data:`DiffFn`: the worktree's tracked changes against HEAD."""
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "diff", "HEAD"], capture_output=True, text=True
    )
    return proc.stdout


@dataclass
class AgentCoder:
    """A bounded, tool-constrained wrapper over the Claude Agent SDK loop.

    Auth (PRD §4a): ``subscription`` runs on the logged-in ``claude`` plan and
    passes **no** API key to the SDK (and asserts the process env has none, so a
    stray key can't silently flip billing to pay-as-you-go); ``api_key`` passes
    the Platform key through.
    """

    model: str
    api_key: str | None = None
    auth_mode: AuthMode = DEFAULT_AUTH_MODE
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    max_turns: int = 25
    max_budget_usd: float | None = None
    query_fn: QueryFn | None = None
    diff_fn: DiffFn | None = None

    def __post_init__(self) -> None:
        if self.query_fn is None:
            self.query_fn = sdk_query
        if self.diff_fn is None:
            self.diff_fn = _git_diff
        if self.auth_mode == "subscription" and os.environ.get("ANTHROPIC_API_KEY"):
            # Defense-in-depth: a stray key in the process env would be inherited
            # by the SDK subprocess and flip billing to pay-as-you-go (PRD §4a).
            raise CoderError(
                "auth_mode=subscription but ANTHROPIC_API_KEY is set in the "
                "environment; unset it so the coder uses plan tokens."
            )
        if self.auth_mode == "api_key" and not self.api_key:
            raise CoderError("auth_mode=api_key requires an api_key.")

    async def _gate(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Fail-closed permission callback: allow the allowlist, deny the rest."""
        if tool_name in self.allowed_tools:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"Tool {tool_name!r} is not allowed for this job.",
            interrupt=False,
        )

    def _options(self, cwd: Path, resume: str | None) -> ClaudeAgentOptions:
        # subscription → no api key in the subprocess env (run on plan tokens);
        # api_key → pass the Platform key through (PRD §4 / §4a).
        env = (
            {"ANTHROPIC_API_KEY": self.api_key}
            if self.auth_mode == "api_key" and self.api_key
            else {}
        )
        return ClaudeAgentOptions(
            model=self.model,
            allowed_tools=list(self.allowed_tools),
            permission_mode="default",
            can_use_tool=self._gate,
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            cwd=str(cwd),
            resume=resume,
            env=env,
        )

    async def _arun(self, prompt: str, cwd: Path, resume: str | None) -> CoderRun:
        assert self.query_fn is not None and self.diff_fn is not None
        options = self._options(cwd, resume)
        turns: list[TurnRecord] = []
        final_text = ""
        result: ResultMessage | None = None
        session_id = resume
        rate_limit_reset: str | None = None
        rate_limited = False

        async for message in self.query_fn(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                tools = tuple(
                    b.name for b in message.content if isinstance(b, ToolUseBlock)
                )
                texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                if texts:
                    final_text = texts[-1]
                turns.append(
                    TurnRecord(index=len(turns) + 1, tools=tools, usage=message.usage)
                )
                if message.session_id:
                    session_id = message.session_id
                if message.error == "rate_limit":
                    rate_limited = True
            elif isinstance(message, RateLimitEvent):
                if message.session_id:
                    session_id = message.session_id
                info = message.rate_limit_info
                if getattr(info, "status", None) == "rejected":
                    rate_limited = True
                    rate_limit_reset = getattr(info, "resets_at", None)
            elif isinstance(message, ResultMessage):
                result = message
                if message.session_id:
                    session_id = message.session_id

        # A rate-limit cutoff is a retryable escalation, not a failure (PRD §4a):
        # surface the session so the caller can resume after the window resets.
        if rate_limited:
            raise RateLimitError(session_id=session_id, resets_at=rate_limit_reset)

        if result is None:
            raise CoderError("Agent SDK produced no terminal result message.")

        return CoderRun(
            diff=self.diff_fn(cwd),
            final_text=final_text or (result.result or ""),
            turns=result.num_turns,
            session_id=result.session_id,
            subtype=result.subtype,
            is_error=result.is_error,
            cost_usd=result.total_cost_usd,
            usage=result.usage,
            turn_records=tuple(turns),
        )

    def run(self, prompt: str, cwd: str | Path, *, resume: str | None = None) -> CoderRun:
        """Run the bounded SDK agent loop in ``cwd`` and capture the outcome."""
        return asyncio.run(self._arun(prompt, Path(cwd), resume))


__all__ = [
    "CLEAN_SUBTYPES",
    "DEFAULT_ALLOWED_TOOLS",
    "AgentCoder",
    "Coder",
    "CoderError",
    "CoderRun",
    "DiffFn",
    "QueryFn",
    "RateLimitError",
    "TurnRecord",
]
