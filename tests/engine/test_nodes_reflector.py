"""Contract tests for the reflector node."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stromboli.engine.nodes.base import NodeOutputError
from stromboli.engine.nodes.reflector import ReflectorNode, ReflectTarget
from tests.engine._fakes import ScriptedCC, empty_state

TARGET = ReflectTarget(
    unit_id="AC-001",
    acceptance_criterion="GET /health returns 200",
    reasons=("test asserts nothing",),
    diff="+++ test_app.py\n+assert True",
)


def test_prompt_includes_reasons_and_diff(tmp_path: Path) -> None:
    cc = ScriptedCC('{"feedback": "add a real assertion"}')
    ReflectorNode(cc).reflect(empty_state(tmp_path), TARGET, tmp_path)
    prompt = cc.prompts[0]
    assert "test asserts nothing" in prompt
    assert "+assert True" in prompt


def test_reflect_returns_feedback(tmp_path: Path) -> None:
    cc = ScriptedCC(
        json.dumps(
            {"feedback": "assert the status code", "replan": False, "replan_reason": ""}
        )
    )
    reflection = ReflectorNode(cc).reflect(empty_state(tmp_path), TARGET, tmp_path)
    assert reflection.feedback == "assert the status code"
    assert reflection.wants_replan is False


def test_reflect_can_request_replan(tmp_path: Path) -> None:
    cc = ScriptedCC(
        json.dumps(
            {
                "feedback": "this unit conflates two concerns",
                "replan": True,
                "replan_reason": "split the endpoint and the docs",
            }
        )
    )
    reflection = ReflectorNode(cc).reflect(empty_state(tmp_path), TARGET, tmp_path)
    assert reflection.wants_replan is True
    assert reflection.replan_reason == "split the endpoint and the docs"


def test_garbled_output_fails_closed(tmp_path: Path) -> None:
    cc = ScriptedCC("hmm let me think", "no json here either")
    with pytest.raises(NodeOutputError):
        ReflectorNode(cc).reflect(empty_state(tmp_path), TARGET, tmp_path)
    assert cc.calls == 2
