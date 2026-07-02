"""Morning rundown → improvement backlog (self-improving §4).

The triage layer over the failure store (§1): cluster unresolved failures by
``task_type`` × ``failure_mode`` and route each cluster to the loop that can fix
its class of problem —

* **missing knowledge** → memory (a lesson/skill should cover it next time);
* **prompt weakness** (the human overruled the verifier) → the GEPA queue;
* **architectural / structural** flaw → a ticket for a human.

Pure functions over :class:`FailureRecord` lists (fully testable, no I/O); the
CLI ``rundown`` subcommand wires them to the failure store, a Telegram digest,
and a ``backlog.md`` for the ticket route.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from stromboli.orchestration.failure_index import LABEL_REJECT, FailureRecord

#: Route targets for a failure cluster.
ROUTE_MEMORY = "memory"
ROUTE_GEPA = "gepa"
ROUTE_TICKET = "ticket"

#: Failure modes that indicate *missing knowledge* — a lesson/skill should have
#: covered it. Routed to memory.
_MEMORY_MODES = frozenset(
    {"missing-tests", "missing-knowledge", "incomplete", "wrong-api", "missing-context"}
)


@dataclass(frozen=True)
class Cluster:
    """A group of failures sharing a task_type × failure_mode, with its route."""

    task_type: str
    failure_mode: str
    count: int
    route: str
    sample_reason: str
    sample_fix: str
    task_ids: tuple[str, ...] = field(default_factory=tuple)


def route_for(records: list[FailureRecord]) -> str:
    """Decide which improvement loop a cluster belongs to.

    Priority: a human *reject* on the verifier's call means the judge was wrong
    (prompt weakness → GEPA); else a missing-knowledge failure mode → memory;
    else it's structural → a ticket.
    """
    if any(r.human_label == LABEL_REJECT for r in records):
        return ROUTE_GEPA
    mode = records[0].failure_mode
    if mode in _MEMORY_MODES:
        return ROUTE_MEMORY
    return ROUTE_TICKET


def cluster_failures(failures: list[FailureRecord]) -> list[Cluster]:
    """Group failures by (task_type, failure_mode), route each, biggest first."""
    groups: dict[tuple[str, str], list[FailureRecord]] = {}
    for rec in failures:
        key = (rec.task_type or "unknown", rec.failure_mode or "unknown")
        groups.setdefault(key, []).append(rec)

    clusters: list[Cluster] = []
    for (task_type, failure_mode), recs in groups.items():
        sample = next((r for r in recs if r.fix), recs[0])
        clusters.append(
            Cluster(
                task_type=task_type,
                failure_mode=failure_mode,
                count=len(recs),
                route=route_for(recs),
                sample_reason=sample.reason,
                sample_fix=sample.fix,
                task_ids=tuple(r.task_id for r in recs),
            )
        )
    clusters.sort(key=lambda c: c.count, reverse=True)
    return clusters


def format_digest(clusters: list[Cluster]) -> str:
    """A Telegram-friendly digest of the clusters and their routing."""
    if not clusters:
        return "🌅 Rundown: no unresolved failures. Nothing to triage."
    routed = Counter(c.route for c in clusters)
    total = sum(c.count for c in clusters)
    lines = [
        f"🌅 Rundown: {total} unresolved failure(s) in {len(clusters)} cluster(s)",
        f"   → {routed[ROUTE_MEMORY]} memory · {routed[ROUTE_GEPA]} GEPA · "
        f"{routed[ROUTE_TICKET]} ticket",
        "",
    ]
    icon = {ROUTE_MEMORY: "🧠", ROUTE_GEPA: "⚖️", ROUTE_TICKET: "🎫"}
    for c in clusters:
        lines.append(
            f"{icon.get(c.route, '•')} [{c.route}] {c.task_type}/{c.failure_mode} "
            f"×{c.count}"
        )
        if c.sample_fix:
            lines.append(f"     fix: {c.sample_fix}")
    return "\n".join(lines)


def format_backlog(clusters: list[Cluster]) -> str:
    """A markdown backlog of the ticket-routed clusters (architectural flaws)."""
    tickets = [c for c in clusters if c.route == ROUTE_TICKET]
    if not tickets:
        return "# Stromboli backlog\n\n_No architectural tickets this run._\n"
    lines = ["# Stromboli backlog\n"]
    for c in tickets:
        lines.append(f"## {c.task_type} / {c.failure_mode} (×{c.count})")
        if c.sample_reason:
            lines.append(f"- **Symptom:** {c.sample_reason}")
        if c.sample_fix:
            lines.append(f"- **Suggested fix:** {c.sample_fix}")
        lines.append(f"- **Tasks:** {', '.join(c.task_ids)}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "ROUTE_GEPA",
    "ROUTE_MEMORY",
    "ROUTE_TICKET",
    "Cluster",
    "cluster_failures",
    "format_backlog",
    "format_digest",
    "route_for",
]
