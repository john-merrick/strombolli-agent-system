"""Stromboli triage as a Prefect 3 flow — the orchestrator comparison (A).

Each phase is a Prefect ``@task`` so the whole triage shows up as a node graph in
the Prefect UI (states, retries, logs, where it broke), and the Notion poll is a
scheduled flow. It delegates to :class:`~stromboli.orchestration.phases.TriagePhases`
so it runs the *real* Stromboli logic (LangGraph nodes + Langfuse) — only the
orchestration/visualization is Prefect's.

Run the UI + a scheduled poll:

    prefect server start                      # the dashboard at :4200
    op run --env-file=.env -- python -m stromboli.orchestration.prefect_flow

(or import ``triage_flow`` / ``poll_flow`` and deploy however you like).
"""

from __future__ import annotations

from prefect import flow, get_run_logger, task

from stromboli.orchestration.phases import TriagePhases
from stromboli.state import StromboliState


@task
def intake(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.intake(state)


@task
def spec(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.spec(state)


@task
def prompt(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.prompt(state)


@task(retries=1)
def coding(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.coding(state)


@task
def verify(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.verify(state)


@task
def open_pr(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.open_pr(state)


@task
def memory_write(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.memory_write(state)


@task
def finalize(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.finalize(state)


@flow(name="stromboli-triage")
def triage_flow(
    task_id: str,
    source: str = "notion",
    *,
    phases: TriagePhases | None = None,
) -> StromboliState:
    """Run one task through the triage phases (the per-task DAG, Prefect-driven)."""
    log = get_run_logger()
    phases = phases or TriagePhases.from_settings()
    state = StromboliState(task_id=task_id, source=source, raw_request="")  # type: ignore[arg-type]

    state = intake(phases, state)
    state = spec(phases, state)
    if phases.is_ambiguous(state):
        return finalize(phases, phases.mark_escalated(state, "spec is ambiguous"))

    state = prompt(phases, state)
    for _ in range(phases.deps.budgets.max_outer_revisions + 1):
        state = coding(phases, state)
        if phases.coding_escalated(state):  # rate-limit cutoff
            return finalize(phases, state)
        state = verify(phases, state)
        route = phases.verdict_route(state)
        if route == "pr":
            state = open_pr(phases, state)
            state = memory_write(phases, state)
            return finalize(phases, state)
        if route == "escalate":
            reason = state.verdict.reason if state.verdict else "escalate"
            return finalize(phases, phases.mark_escalated(state, reason))
        log.info("revise cycle %d", state.outer_iterations)

    return finalize(phases, phases.mark_escalated(state, "revision cap reached"))


@flow(name="stromboli-poll")
def poll_flow(phases: TriagePhases | None = None) -> int:
    """Poll Notion and trigger a triage flow per Ready task (the front-end)."""
    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.settings import load_settings

    settings = load_settings()
    notion = NotionTaskClient(settings.notion_token)
    tasks = notion.query_ready_tasks(settings.notion_task_db_id)
    built = phases or TriagePhases.from_settings(settings)
    for t in tasks:
        triage_flow(t.page_id, source="notion", phases=built)
    return len(tasks)


def serve(interval: float = 30.0) -> None:
    """Serve the poll flow on a schedule (creates a deployment + worker)."""
    poll_flow.serve(name="stromboli-poll", interval=interval)


if __name__ == "__main__":
    serve()


__all__ = ["poll_flow", "serve", "triage_flow"]
