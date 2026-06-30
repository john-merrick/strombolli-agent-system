"""The single typed state object threaded through every graph node (PRD §5).

One narrow, semantic Pydantic model — :class:`StromboliState` — is the only
thing that flows between nodes. LangGraph merges each node's returned partial
state onto it; for the two **append-only** fields (``test_results`` and
``reflections``) we attach an ``operator.add`` reducer via :data:`typing.Annotated`
so successive nodes *accumulate* rather than overwrite. Every other field
overwrites (LangGraph's default last-write-wins).

The nested models (:class:`Spec`, :class:`Verdict`, :class:`TestResult`) double
as the **structured-output schemas** the LiteLLM gateway nodes coerce their LLM
calls into (PRD §4), so the contract a node returns and the contract the graph
stores are the same type.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

#: The lifecycle a task moves through, surfaced for status / tracing.
#: ``queued`` is a *suspended* task awaiting human input via the investigate
#: loop (PRD investigate-loop design) — distinct from ``escalated`` (terminal).
Status = Literal[
    "intake", "specced", "coding", "verifying", "pr", "done", "escalated", "queued"
]
#: Where a task originated — an Intake source (PRD §6.1).
Source = Literal["notion", "telegram", "cli"]
#: The verifier's decision (PRD §6.5 / §6.6).
Decision = Literal["pass", "revise", "escalate"]


class TestResult(BaseModel):
    """One sandbox test/compiler run, captured by the coding node (append-only)."""

    passed: bool
    summary: str
    raw: str = ""  # truncated test/compiler output


class Spec(BaseModel):
    """The structured task specification the Spec node produces (PRD §6.2)."""

    goal: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    #: The router reads this — true when criteria can't be pinned down.
    ambiguous: bool = False


class Verdict(BaseModel):
    """The verifier's structured judgment of the diff (PRD §6.5)."""

    decision: Decision
    reason: str
    #: Did the tests actually cover what the spec cared about?
    coverage_note: str = ""


class StromboliState(BaseModel):
    """The whole task state — one object, threaded through every node (PRD §5)."""

    task_id: str
    source: Source
    raw_request: str

    spec: Spec | None = None
    plan: str | None = None
    code_diff: str | None = None

    #: Append-only: every sandbox run accumulates here (reducer = list add).
    test_results: Annotated[list[TestResult], operator.add] = Field(
        default_factory=list
    )
    #: Append-only: verifier reflections accumulate here (reducer = list add).
    reflections: Annotated[list[str], operator.add] = Field(default_factory=list)

    #: The Agent SDK agent-loop turns spent on the latest coding attempt.
    inner_iterations: int = 0
    #: The number of verifier revise edges taken (outer recursion).
    outer_iterations: int = 0

    verdict: Verdict | None = None
    #: IDs of memory entries retrieved for this task (for tracing / decay).
    memory_refs: list[str] = Field(default_factory=list)
    #: The Agent SDK session id, captured so a revise pass can ``--resume``.
    session_id: str | None = None
    pr_url: str | None = None
    status: Status = "intake"


__all__ = [
    "Decision",
    "Source",
    "Spec",
    "Status",
    "StromboliState",
    "TestResult",
    "Verdict",
]
