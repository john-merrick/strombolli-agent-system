"""Contract tests for the worker node."""

from __future__ import annotations

from pathlib import Path

from stromboli.engine.nodes.worker import WorkerNode
from stromboli.engine.state import BuildState, Plan, Unit
from tests.engine._fakes import ScriptedCC


def _unit() -> Unit:
    return Unit(
        id="AC-001",
        description="add endpoint",
        acceptance_criterion="GET /health returns 200",
        definition_of_done="a test covers it",
    )


def _state(root: Path, *, feedback: str = "") -> BuildState:
    return BuildState(plan=Plan(version=1, units=[_unit()]), root=root, feedback=feedback)


def test_prompt_includes_criterion_and_dod(tmp_path: Path) -> None:
    cc = ScriptedCC("done")
    WorkerNode(cc).work(_state(tmp_path), _unit(), tmp_path)
    prompt = cc.prompts[0]
    assert "GET /health returns 200" in prompt
    assert "a test covers it" in prompt


def test_prompt_includes_feedback_when_present(tmp_path: Path) -> None:
    cc = ScriptedCC("done")
    WorkerNode(cc).work(_state(tmp_path, feedback="use a real assertion"), _unit(), tmp_path)
    assert "use a real assertion" in cc.prompts[0]


def test_prompt_omits_feedback_section_when_empty(tmp_path: Path) -> None:
    cc = ScriptedCC("done")
    WorkerNode(cc).work(_state(tmp_path), _unit(), tmp_path)
    assert "Reviewer feedback" not in cc.prompts[0]


def test_work_returns_agent_summary(tmp_path: Path) -> None:
    cc = ScriptedCC("edited app.py and added a test")
    summary = WorkerNode(cc).work(_state(tmp_path), _unit(), tmp_path)
    assert summary == "edited app.py and added a test"
    assert cc.calls == 1
