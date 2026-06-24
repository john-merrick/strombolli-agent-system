"""Contract tests for the verifier node."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stromboli.engine.nodes.base import NodeOutputError
from stromboli.engine.nodes.verifier import VerifierNode, VerifyTarget
from stromboli.engine.state import WHOLE_DIFF
from tests.engine._fakes import ScriptedCC, empty_state

TARGET = VerifyTarget(
    scope="AC-001",
    acceptance_criterion="GET /health returns 200",
    diff="+++ app.py\n+@app.get('/health')",
    definition_of_done="a test covers it",
)


def test_prompt_includes_criterion_and_diff(tmp_path: Path) -> None:
    cc = ScriptedCC('{"met": true}')
    VerifierNode(cc).verify(empty_state(tmp_path), TARGET, tmp_path)
    prompt = cc.prompts[0]
    assert "GET /health returns 200" in prompt
    assert "+@app.get('/health')" in prompt
    assert "hollow" in prompt.lower()


def test_verify_returns_scoped_verdict(tmp_path: Path) -> None:
    cc = ScriptedCC(json.dumps({"met": True, "hollow_tests": False, "reasons": []}))
    verdict = VerifierNode(cc).verify(empty_state(tmp_path), TARGET, tmp_path)
    assert verdict.scope == "AC-001"
    assert verdict.met is True


def test_verify_propagates_hollow_tests_and_reasons(tmp_path: Path) -> None:
    cc = ScriptedCC(
        json.dumps(
            {"met": False, "hollow_tests": True, "reasons": ["assert True only"]}
        )
    )
    verdict = VerifierNode(cc).verify(empty_state(tmp_path), TARGET, tmp_path)
    assert verdict.met is False
    assert verdict.hollow_tests is True
    assert verdict.reasons == ("assert True only",)


def test_whole_diff_scope_is_carried(tmp_path: Path) -> None:
    whole = VerifyTarget(scope=WHOLE_DIFF, acceptance_criterion="all", diff="d")
    cc = ScriptedCC('{"met": true}')
    verdict = VerifierNode(cc).verify(empty_state(tmp_path), whole, tmp_path)
    assert verdict.scope == WHOLE_DIFF


def test_garbled_output_never_auto_passes(tmp_path: Path) -> None:
    # The verifier must fail closed rather than default to met=true.
    cc = ScriptedCC("looks good to me!", "still prose, no json")
    with pytest.raises(NodeOutputError):
        VerifierNode(cc).verify(empty_state(tmp_path), TARGET, tmp_path)
    assert cc.calls == 2
