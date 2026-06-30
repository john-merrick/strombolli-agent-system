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
from prefect.cache_policies import NO_CACHE

from stromboli.orchestration.phases import TriagePhases
from stromboli.state import StromboliState

# The TriagePhases arg carries live objects (tracer, locks, clients) that can't
# be hashed or pickled, so Prefect's default input-hash cache key blows up. We
# don't want result caching here anyway — every phase has side effects — so
# disable it explicitly on every task.


@task(cache_policy=NO_CACHE)
def intake(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.intake(state)


@task(cache_policy=NO_CACHE)
def spec(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.spec(state)


@task(cache_policy=NO_CACHE)
def prompt(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.prompt(state)


@task(retries=1, cache_policy=NO_CACHE)
def coding(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.coding(state)


@task(cache_policy=NO_CACHE)
def verify(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.verify(state)


@task(cache_policy=NO_CACHE)
def open_pr(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.open_pr(state)


@task(cache_policy=NO_CACHE)
def memory_write(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.memory_write(state)


@task(cache_policy=NO_CACHE)
def finalize(phases: TriagePhases, state: StromboliState) -> StromboliState:
    return phases.finalize(state)


@task(cache_policy=NO_CACHE)
def suspend(phases: TriagePhases, state: StromboliState, reason: str) -> StromboliState:
    return phases.suspend(state, reason)


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
    # Lane-B escalations suspend (Queued) for the investigate loop, not finalize.
    if phases.is_ambiguous(state):
        return suspend(phases, state, "spec is ambiguous")

    state = prompt(phases, state)
    for _ in range(phases.deps.budgets.max_outer_revisions + 1):
        state = coding(phases, state)
        if phases.coding_escalated(state):  # rate-limit cutoff (auto lane, not Lane B)
            return finalize(phases, state)
        state = verify(phases, state)
        route = phases.verdict_route(state)
        if route == "pr":
            state = open_pr(phases, state)
            state = memory_write(phases, state)
            return finalize(phases, state)
        if route == "escalate":
            reason = state.verdict.reason if state.verdict else "escalate"
            return suspend(phases, state, reason)
        log.info("revise cycle %d", state.outer_iterations)

    return suspend(phases, state, "revision cap reached")


# Tasks already announced to Telegram, so a row isn't re-pinged across polls.
_ANNOUNCED: set[str] = set()
# Tasks being built right now in this process. Since a task in ``Working on`` is
# now dispatchable (so crashed runs get retried), this is what stops the 30s
# poll from re-dispatching a task that's still mid-build in this worker.
_IN_FLIGHT: set[str] = set()


@flow(name="stromboli-poll")
def poll_flow(phases: TriagePhases | None = None) -> int:
    """Poll Notion and trigger a triage flow per dispatchable task (the front-end).

    Each newly-seen task is flagged to Telegram (name + id) before it's built,
    so the watcher monitors out loud — same signal as the ``watch`` daemon. A
    task already in flight in this process is skipped, so the recurring poll
    doesn't double-dispatch a long-running build.
    """
    from datetime import datetime

    from stromboli.integrations.notion import NotionTaskClient
    from stromboli.settings import load_settings

    log = get_run_logger()
    settings = load_settings()
    notion = NotionTaskClient(settings.notion_token)
    built = phases or TriagePhases.from_settings(settings)
    notifier = built.deps.notifier

    tasks = notion.query_ready_tasks(settings.notion_task_db_id)
    dispatched = 0
    for t in tasks:
        if t.page_id in _IN_FLIGHT:
            log.info("skip %s (%s): already in flight", t.name, t.page_id)
            continue
        if t.page_id not in _ANNOUNCED:
            _ANNOUNCED.add(t.page_id)
            now = datetime.now().isoformat(timespec="seconds")
            notifier.notify(f"🆕 New task picked up: {t.name} ({t.page_id}) at {now}")
            log.info("dispatching %s (%s)", t.name, t.page_id)
        _IN_FLIGHT.add(t.page_id)
        try:
            triage_flow(t.page_id, source="notion", phases=built)
            dispatched += 1
        finally:
            _IN_FLIGHT.discard(t.page_id)
    return dispatched


def serve(interval: float = 30.0) -> None:
    """Serve the poll flow on a schedule (creates a deployment + worker).

    ``limit=1`` serialises the runner: only one poll runs at a time, and since
    each poll blocks on its triage subflows, the next poll can't start until the
    current task reaches a terminal status. That's the real guard against
    double-dispatch — ``serve`` runs each scheduled poll in its own subprocess,
    so the in-process ``_IN_FLIGHT`` set alone wouldn't catch an overlap.
    """
    from stromboli.orchestration.phases import TriagePhases

    TriagePhases.from_settings().deps.notifier.notify(
        "👀 Stromboli (Prefect) is watching the Notion queue."
    )
    poll_flow.serve(name="stromboli-poll", interval=interval, limit=1)


if __name__ == "__main__":
    serve()


__all__ = ["poll_flow", "serve", "triage_flow"]
