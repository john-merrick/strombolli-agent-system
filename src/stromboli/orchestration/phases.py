"""The Stromboli triage phases as orchestrator-agnostic ``state -> state`` steps.

Each method runs one real graph node (built from :class:`GraphDeps`) against the
current :class:`StromboliState` and returns the merged next state — honouring the
append-only reducers (``test_results``, ``reflections``). Routing helpers expose
the same decisions the LangGraph conditional edges make, so an external
orchestrator can wire the flow (and show it in its dashboard) using the real
Stromboli logic rather than a copy.

This is shared by the Prefect flow and the Windmill flow so the comparison is
between the *tools*, not two different pipelines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stromboli.nodes import (
    make_coding,
    make_human,
    make_intake,
    make_memory_write,
    make_pr,
    make_spec,
    make_verifier,
)
from stromboli.nodes.prompt import make_prompt
from stromboli.state import StromboliState

#: Fields with append-reducers (must concatenate, not overwrite).
_APPEND_FIELDS = ("test_results", "reflections")


@dataclass
class TriagePhases:
    """Phase steps + routing helpers over a :class:`GraphDeps`."""

    deps: Any  # GraphDeps — typed Any to avoid a heavy import cycle

    # Built node callables (one per phase).
    _intake: Any = field(init=False)
    _spec: Any = field(init=False)
    _prompt: Any = field(init=False)
    _coding: Any = field(init=False)
    _verifier: Any = field(init=False)
    _pr: Any = field(init=False)
    _human: Any = field(init=False)
    _memory: Any = field(init=False)

    def __post_init__(self) -> None:
        d = self.deps
        self._intake = make_intake(notion=d.notion)
        retriever = d.memory.recall_for_spec if d.memory is not None else None
        self._spec = make_spec(d.gateway, model=d.reasoning_model, retriever=retriever)
        self._prompt = make_prompt(d.gateway, model=d.prompt_model, notion=d.notion)
        self._coding = make_coding(d.coder, d.sandbox, d.worktree_for)
        self._verifier = make_verifier(d.gateway, model=d.verifier_model)
        self._pr = make_pr(
            github=d.github, notion=d.notion, worktree_for=d.worktree_for,
            base=d.base_branch, dry_run=d.dry_run_pr, git_run=d.git_run,
        )
        self._human = make_human(notifier=d.notifier, notion=d.notion)
        self._memory = make_memory_write(d.memory)

    @classmethod
    def from_settings(cls, settings: Any = None) -> TriagePhases:
        """Build production phases from env-backed settings."""
        from stromboli.graph import _deps_from_settings
        from stromboli.settings import load_settings

        return cls(_deps_from_settings(settings or load_settings()))

    # -- step plumbing ----------------------------------------------------- #
    def _merge(self, state: StromboliState, partial: dict[str, Any]) -> StromboliState:
        data = state.model_dump()
        for key, value in partial.items():
            if key in _APPEND_FIELDS:
                data[key] = list(data.get(key) or []) + list(value)
            else:
                data[key] = value
        return StromboliState.model_validate(data)

    # -- phases (state -> state) ------------------------------------------- #
    def intake(self, state: StromboliState) -> StromboliState:
        return self._merge(state, self._intake(state))

    def spec(self, state: StromboliState) -> StromboliState:
        return self._merge(state, self._spec(state))

    def prompt(self, state: StromboliState) -> StromboliState:
        return self._merge(state, self._prompt(state))

    def coding(self, state: StromboliState) -> StromboliState:
        return self._merge(state, self._coding(state))

    def verify(self, state: StromboliState) -> StromboliState:
        out = self._verifier(state)
        merged = self._merge(state, out)
        # Mirror the graph's outer-recursion counter on a revise verdict.
        if merged.verdict is not None and merged.verdict.decision == "revise":
            merged = merged.model_copy(
                update={"outer_iterations": merged.outer_iterations + 1}
            )
        return merged

    def open_pr(self, state: StromboliState) -> StromboliState:
        return self._merge(state, self._pr(state))

    def memory_write(self, state: StromboliState) -> StromboliState:
        return self._merge(state, self._memory(state))

    # -- routing helpers (mirror the conditional edges) -------------------- #
    @staticmethod
    def is_ambiguous(state: StromboliState) -> bool:
        return state.spec is not None and state.spec.ambiguous

    @staticmethod
    def coding_escalated(state: StromboliState) -> bool:
        return state.status == "escalated"  # rate-limit cutoff (PRD §4a)

    def verdict_route(self, state: StromboliState) -> str:
        """``pass`` → pr, ``revise`` (under cap) → coding, else → escalate."""
        verdict = state.verdict
        if verdict is None:
            return "escalate"
        if verdict.decision == "pass":
            return "pr"
        cap = self.deps.budgets.max_outer_revisions
        if verdict.decision == "revise" and state.outer_iterations < cap:
            return "coding"
        return "escalate"

    # -- terminal handling (external orchestrators have no interrupt()) ----- #
    def mark_escalated(self, state: StromboliState, reason: str) -> StromboliState:
        """Set the escalated status + reason (the orchestrator then finalizes)."""
        return state.model_copy(
            update={"status": "escalated", "reflections": [*state.reflections, reason]}
        )

    # -- suspend (the investigate loop; design DL-2) ----------------------- #
    def _paused_index(self) -> Any:
        """The injected paused index, or one built from ``workspace_root`` (or None)."""
        if self.deps.paused_index is not None:
            return self.deps.paused_index
        root = self.deps.workspace_root
        if root is None:
            return None
        from pathlib import Path

        from stromboli.orchestration.paused import PausedIndex

        return PausedIndex(Path(root) / ".stromboli" / "paused.db")

    def suspend(self, state: StromboliState, reason: str) -> StromboliState:
        """Suspend a Lane-B escalation for the investigate loop, instead of parking it.

        Persists the full state (so the run is resumable in place), assigns a
        short ``#N`` handle, and flips Notion to ``Queued`` (non-dispatchable, so
        the poll won't re-grab it). The run is *not* finalized — a human resumes
        it later via the investigate loop.
        """
        from stromboli.integrations.notion import STATUS_QUEUED

        queued = state.model_copy(
            update={"status": "queued", "reflections": [*state.reflections, reason]}
        )
        name = state.spec.goal if state.spec is not None else (state.raw_request[:60])
        index = self._paused_index()
        ref: int | None = None
        if index is not None:
            ref = index.suspend(queued, reason=reason, name=name).ref
        notion = self.deps.notion
        if notion is not None and state.source == "notion":
            try:
                notion.update_task(state.task_id, status=STATUS_QUEUED)
            except Exception:  # noqa: BLE001 - status write must never crash
                pass
        self._send_opener(ref, name, reason)
        return queued

    def resume_with_guidance(
        self, state: StromboliState, guidance: str
    ) -> StromboliState:
        """Resume a suspended task once with human guidance (design DL-13).

        Folds the guidance into the coding prompt + reflections and runs a single
        coding→verify cycle: ``pass`` → PR + memory (done); anything else →
        ``escalated`` (Review). A second failure is **not** re-queued — it goes to
        a human, so the loop can't cycle forever. Terminal I/O (Notion/Telegram)
        is the caller's ``finalize``; this method only computes the next state.
        """
        merged = state.model_copy(
            update={
                "plan": f"{state.plan or ''}\n\nHuman guidance: {guidance}".strip(),
                "reflections": [*state.reflections, f"resume guidance: {guidance}"],
                "status": "coding",
            }
        )
        merged = self.coding(merged)
        if self.coding_escalated(merged):  # rate-limited again mid-resume
            return self.mark_escalated(merged, "rate-limited on resume")
        merged = self.verify(merged)
        if merged.verdict is not None and merged.verdict.decision == "pass":
            merged = self.open_pr(merged)
            return self.memory_write(merged)
        reason = merged.verdict.reason if merged.verdict is not None else "no verdict"
        return self.mark_escalated(merged, f"still failing after guidance: {reason}")

    def _send_opener(self, ref: int | None, name: str, reason: str) -> None:
        """Post the investigate-bot opener that starts the resolution chat."""
        send = self.deps.investigate_notify
        if send is None or ref is None:
            return
        try:
            send(
                f"🔎 #{ref} — {name} queued.\nReason: {reason}\n"
                f"Reply `#{ref} <message>` to dig in, `#{ref} ✅` to apply, "
                f"`/queued` to list."
            )
        except Exception:  # noqa: BLE001 - a notification must never break a run
            pass

    def finalize(self, state: StromboliState) -> StromboliState:
        """Terminal write-back: Notion status + summary + Telegram (PRD §6.7/§6.9).

        ``done`` → Complete + "done"; anything else → Review + escalation. Mirrors
        the graph's finalize so the orchestrated flow behaves identically.
        """
        from stromboli.integrations.notion import (
            build_feedback_summary,
            resilient_append,
        )

        done = state.status == "done"
        notion = self.deps.notion
        if notion is not None and state.source == "notion":
            summary = build_feedback_summary(
                status=state.status, pr_url=state.pr_url,
                reflections=state.reflections,
                coverage_note=state.verdict.coverage_note if state.verdict else "",
            )
            resilient_append(notion, state.task_id, summary)
            try:
                notion.update_task(
                    state.task_id, status="Complete" if done else "Review"
                )
            except Exception:  # noqa: BLE001 - write-back must never crash
                pass
        if done:
            self.deps.notifier.done(state.task_id, state.pr_url)
        else:
            reason = state.reflections[-1] if state.reflections else "needs review"
            self.deps.notifier.escalation(state.task_id, reason)
        # Terminal outcome → free the durable worktree (suspend never calls
        # finalize, so a Queued task's worktree survives for resume/probe).
        cleanup = self.deps.worktree_cleanup
        if cleanup is not None:
            try:
                cleanup(state.task_id)
            except Exception:  # noqa: BLE001 - cleanup must never break finalize
                pass
        return state


__all__ = ["TriagePhases"]
