"""Contract tests for the planner node."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stromboli.engine.nodes.base import NodeOutputError
from stromboli.engine.nodes.planner import PlannerNode
from stromboli.engine.state import BuildState, Plan, UnitStatus
from tests.engine._fakes import ScriptedCC, empty_state

SPEC = "Build a /health endpoint that returns 200 and document it."


def _plan_json(*units: dict[str, str]) -> str:
    return json.dumps({"units": list(units)})


def test_prompt_includes_the_spec(tmp_path: Path) -> None:
    cc = ScriptedCC(_plan_json({"description": "d", "acceptance_criterion": "a"}))
    node = PlannerNode(cc)
    node.plan(empty_state(tmp_path), SPEC, tmp_path)
    assert SPEC in cc.prompts[0]


def test_prompt_includes_replan_feedback(tmp_path: Path) -> None:
    cc = ScriptedCC(_plan_json({"description": "d", "acceptance_criterion": "a"}))
    state = empty_state(tmp_path)
    state.pending_replan = "units were too coarse"
    PlannerNode(cc).plan(state, SPEC, tmp_path)
    assert "units were too coarse" in cc.prompts[0]


def test_plan_builds_versioned_units_with_ids(tmp_path: Path) -> None:
    cc = ScriptedCC(
        _plan_json(
            {"description": "endpoint", "acceptance_criterion": "200 on /health"},
            {
                "description": "docs",
                "acceptance_criterion": "documented",
                "definition_of_done": "README updated",
            },
        )
    )
    plan = PlannerNode(cc).plan(empty_state(tmp_path), SPEC, tmp_path)
    assert plan.version == 1
    assert [u.id for u in plan.units] == ["AC-001", "AC-002"]
    assert plan.units[0].acceptance_criterion == "200 on /health"
    assert plan.units[1].definition_of_done == "README updated"
    assert all(u.status is UnitStatus.PENDING for u in plan.units)


def test_replan_bumps_the_version(tmp_path: Path) -> None:
    cc = ScriptedCC(_plan_json({"description": "d", "acceptance_criterion": "a"}))
    state = BuildState(plan=Plan(version=2, units=[]), root=tmp_path)
    plan = PlannerNode(cc).plan(state, SPEC, tmp_path)
    assert plan.version == 3


def test_empty_unit_list_fails_closed(tmp_path: Path) -> None:
    # PlanModel requires min_length=1 → schema reject → re-prompt → still bad → raise.
    cc = ScriptedCC('{"units": []}', '{"units": []}')
    with pytest.raises(NodeOutputError):
        PlannerNode(cc).plan(empty_state(tmp_path), SPEC, tmp_path)
    assert cc.calls == 2
