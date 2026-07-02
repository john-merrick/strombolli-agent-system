"""Tests for the rundown/triage layer (self-improving §4)."""

from __future__ import annotations

from typing import Any

from stromboli.orchestration.failure_index import LABEL_REJECT, FailureRecord
from stromboli.orchestration.rundown import (
    ROUTE_GEPA,
    ROUTE_MEMORY,
    ROUTE_TICKET,
    cluster_failures,
    format_backlog,
    format_digest,
    route_for,
)


def _rec(task_id: str, task_type: str, failure_mode: str, **over: object) -> FailureRecord:
    base: dict[str, Any] = dict(
        task_id=task_id, ts="t", goal="g", decision="revise", outcome="escalated",
        task_type=task_type, failure_mode=failure_mode, reason="r", fix="do the thing",
    )
    base.update(over)
    return FailureRecord(**base)


def test_route_missing_knowledge_to_memory() -> None:
    assert route_for([_rec("a", "add-endpoint", "missing-tests")]) == ROUTE_MEMORY


def test_route_human_reject_to_gepa() -> None:
    recs = [_rec("a", "bugfix", "missing-tests", human_label=LABEL_REJECT)]
    # A human reject overrides the memory routing → the judge was wrong.
    assert route_for(recs) == ROUTE_GEPA


def test_route_structural_to_ticket() -> None:
    assert route_for([_rec("a", "refactor", "architectural")]) == ROUTE_TICKET
    assert route_for([_rec("a", "x", "empty-diff")]) == ROUTE_TICKET


def test_cluster_groups_and_orders_by_count() -> None:
    failures = [
        _rec("a", "add-endpoint", "missing-tests"),
        _rec("b", "add-endpoint", "missing-tests"),
        _rec("c", "refactor", "architectural"),
    ]
    clusters = cluster_failures(failures)
    assert clusters[0].count == 2  # biggest cluster first
    assert clusters[0].task_type == "add-endpoint"
    assert clusters[0].route == ROUTE_MEMORY
    assert clusters[0].task_ids == ("a", "b")
    assert clusters[1].route == ROUTE_TICKET


def test_digest_summarizes_routes() -> None:
    failures = [
        _rec("a", "add-endpoint", "missing-tests"),
        _rec("c", "refactor", "architectural"),
    ]
    digest = format_digest(cluster_failures(failures))
    assert "1 memory" in digest and "1 ticket" in digest
    assert "add-endpoint/missing-tests" in digest


def test_digest_empty() -> None:
    assert "no unresolved failures" in format_digest([]).lower()


def test_backlog_lists_only_tickets() -> None:
    failures = [
        _rec("a", "add-endpoint", "missing-tests"),   # memory — excluded
        _rec("c", "refactor", "architectural", reason="tangled deps"),  # ticket
    ]
    backlog = format_backlog(cluster_failures(failures))
    assert "refactor / architectural" in backlog
    assert "tangled deps" in backlog
    assert "add-endpoint" not in backlog  # memory-routed not in the backlog
