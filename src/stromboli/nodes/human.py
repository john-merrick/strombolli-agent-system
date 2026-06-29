"""Human Interrupt node (PRD §6.8) — pause for human input on ambiguity/stuck.

Calls LangGraph ``interrupt()`` to pause the graph (persisting via the
checkpointer), after surfacing the question/escalation to Telegram + Notion
(best-effort). The graph resumes when the caller re-invokes with a
``Command(resume=...)`` carrying the human's answer. Reached from the Router
(ambiguous spec) or the Verdict gate (escalate / budget exhausted).

The resume payload may carry ``{"raw_request": "..."}`` to re-spec a clarified
task, or ``{"action": "abort"}`` to give up — otherwise the task is marked
``escalated`` and ends.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from stromboli.integrations.notion import AppendGateway, resilient_append
from stromboli.integrations.telegram import Notifier, NullNotifier
from stromboli.nodes.intake import Node
from stromboli.state import StromboliState

logger = logging.getLogger(__name__)


def _reason(state: StromboliState) -> str:
    """Why we're escalating — the spec ambiguity or the verifier's last word."""
    if state.spec is not None and state.spec.ambiguous:
        return f"Spec is ambiguous: {state.spec.goal}"
    if state.verdict is not None:
        return f"Verifier {state.verdict.decision}: {state.verdict.reason}"
    return "Task needs human attention."


def make_human(
    *,
    notifier: Notifier | None = None,
    notion: AppendGateway | None = None,
) -> Node:
    """Build the human-interrupt node."""
    notifier = notifier or NullNotifier()

    def human(state: StromboliState) -> dict[str, object]:
        reason = _reason(state)
        notifier.escalation(state.task_id, reason)
        if notion is not None:
            resilient_append(
                notion, state.task_id, f"🚨 **Stromboli needs you:** {reason}"
            )
        logger.info("Interrupting for human input on task %s.", state.task_id)

        # Pauses here until the caller resumes with Command(resume=<payload>).
        answer: Any = interrupt({"task_id": state.task_id, "reason": reason})

        # A human can mark the escalation resolved; otherwise it stays escalated.
        if isinstance(answer, dict) and answer.get("action") == "resolved":
            return {"status": "done"}
        return {"status": "escalated"}

    return human


__all__ = ["make_human"]
