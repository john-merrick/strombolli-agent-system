"""Tests for converting a task Spec into a Ralph prd.json (US-006).

The compiler turns the task's free-text Spec (a markdown string holding the
acceptance criteria) into a ``{meta, items}`` envelope written into the worktree
where the Ralph loop will read it. The central acceptance claim — *N acceptance
criteria produce N well-formed items* — is asserted directly, alongside the
meta wiring (``maxAttempts`` and ``branchName`` matching the US-005 branch),
criterion-parsing edge cases, and the on-disk write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stromboli.notion import Task
from stromboli.prd import (
    DEFAULT_MAX_ATTEMPTS,
    PRD_RELATIVE_PATH,
    build_prd,
    parse_acceptance_criteria,
    write_prd,
)


def make_task(spec: str, *, name: str = "Add login flow") -> Task:
    """A minimal :class:`Task` carrying only the fields the compiler reads."""
    return Task(
        page_id="page-123",
        name=name,
        project_ids=("proj-1",),
        status="To do",
        spec=spec,
        assigned_to="Agent",
        ready=True,
        needs_review=False,
        pr_url=None,
        cost=None,
        tokens=None,
    )


# --------------------------------------------------------------------------- #
# parse_acceptance_criteria                                                   #
# --------------------------------------------------------------------------- #


def test_parses_dash_bullets() -> None:
    spec = "- First criterion\n- Second criterion\n- Third criterion"
    assert parse_acceptance_criteria(spec) == [
        "First criterion",
        "Second criterion",
        "Third criterion",
    ]


def test_parses_numbered_and_star_and_plus_markers() -> None:
    spec = "1. one\n2) two\n* three\n+ four"
    assert parse_acceptance_criteria(spec) == ["one", "two", "three", "four"]


def test_scopes_to_acceptance_criteria_heading() -> None:
    spec = (
        "# Overview\n"
        "- this bullet is context, not a criterion\n\n"
        "## Acceptance Criteria\n"
        "- real one\n"
        "- real two\n\n"
        "## Notes\n"
        "- ignored trailing bullet\n"
    )
    assert parse_acceptance_criteria(spec) == ["real one", "real two"]


def test_falls_back_to_nonblank_lines_when_no_list_markers() -> None:
    spec = "Do the thing\n\nThen verify the thing\n"
    assert parse_acceptance_criteria(spec) == ["Do the thing", "Then verify the thing"]


def test_ignores_heading_lines_in_fallback() -> None:
    spec = "## Acceptance Criteria\nThe API returns 200"
    assert parse_acceptance_criteria(spec) == ["The API returns 200"]


def test_empty_spec_yields_no_criteria() -> None:
    assert parse_acceptance_criteria("") == []
    assert parse_acceptance_criteria("   \n\n  ") == []


# --------------------------------------------------------------------------- #
# build_prd — the core acceptance claim                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n", [1, 3, 5])
def test_n_criteria_produce_n_well_formed_items(n: int) -> None:
    spec = "\n".join(f"- criterion {i}" for i in range(1, n + 1))
    prd = build_prd(make_task(spec), branch="stromboli/page-123-add-login-flow")

    items = prd["items"]
    assert len(items) == n
    for i, item in enumerate(items, start=1):
        assert item["description"] == f"criterion {i}"
        assert item["passes"] is False
        assert item["attempts"] == 0
        assert item["blocked"] is False
        assert item["blockReason"] == ""
        assert item["id"]  # stable, non-empty id


def test_item_ids_are_unique_and_ordered() -> None:
    spec = "- a\n- b\n- c"
    items = build_prd(make_task(spec), branch="stromboli/x")["items"]
    ids = [it["id"] for it in items]
    assert len(set(ids)) == len(ids)


def test_meta_carries_branch_and_max_attempts() -> None:
    branch = "stromboli/page-123-add-login-flow"
    prd = build_prd(make_task("- only one"), branch=branch, max_attempts=7)
    assert prd["meta"]["branchName"] == branch
    assert prd["meta"]["maxAttempts"] == 7


def test_meta_branch_matches_us005_derived_branch() -> None:
    # The branch passed in is exactly the US-005 worktree branch.
    from stromboli.worktree import derive_branch_name

    task = make_task("- only one", name="Add login flow")
    branch = derive_branch_name(task.page_id, task.name)
    prd = build_prd(task, branch=branch)
    assert prd["meta"]["branchName"] == branch == "stromboli/page-123-add-login-flow"


def test_default_max_attempts_used_when_unspecified() -> None:
    prd = build_prd(make_task("- one"), branch="stromboli/x")
    assert prd["meta"]["maxAttempts"] == DEFAULT_MAX_ATTEMPTS


def test_build_prd_raises_when_no_criteria() -> None:
    with pytest.raises(ValueError, match="no acceptance criteria"):
        build_prd(make_task(""), branch="stromboli/x")


# --------------------------------------------------------------------------- #
# write_prd                                                                    #
# --------------------------------------------------------------------------- #


def test_write_prd_writes_valid_json_at_expected_path(tmp_path: Path) -> None:
    prd = build_prd(make_task("- a\n- b"), branch="stromboli/x")
    target = write_prd(tmp_path, prd)

    assert target == tmp_path / PRD_RELATIVE_PATH
    assert target.exists()
    reloaded = json.loads(target.read_text())
    assert reloaded == prd
    assert len(reloaded["items"]) == 2


def test_write_prd_creates_parent_directories(tmp_path: Path) -> None:
    prd = build_prd(make_task("- a"), branch="stromboli/x")
    target = write_prd(tmp_path / "fresh" / "worktree", prd)
    assert target.exists()
