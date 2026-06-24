"""Tests for the AgentNode seam: parse, fail-closed re-prompt, JSON extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from stromboli.engine.nodes.base import (
    AgentNode,
    NodeOutputError,
    extract_json_object,
    result_text,
)
from stromboli.engine.nodes.schemas import VerdictModel
from stromboli.engine.state import BuildState
from tests.engine._fakes import ScriptedCC, cc_envelope, empty_state


class _VerdictNode(AgentNode[str, VerdictModel]):
    def __init__(self, run: object) -> None:
        super().__init__(run, VerdictModel, name="probe")  # type: ignore[arg-type]

    def build_prompt(self, state: BuildState, target: str) -> str:
        return f"judge: {target}"


def _node(*answers: str) -> tuple[_VerdictNode, ScriptedCC]:
    cc = ScriptedCC(*answers)
    return _VerdictNode(cc), cc


# --------------------------------------------------------------------------- #
# result_text / extract_json_object                                           #
# --------------------------------------------------------------------------- #
def test_result_text_unwraps_envelope() -> None:
    assert result_text(cc_envelope("hello")) == "hello"


def test_result_text_passes_through_non_json() -> None:
    assert result_text("not json at all") == "not json at all"


def test_extract_json_object_strips_code_fences() -> None:
    text = '```json\n{"met": true}\n```'
    assert extract_json_object(text) == '{"met": true}'


def test_extract_json_object_finds_embedded_object() -> None:
    text = 'Sure! Here you go: {"met": false, "reasons": ["x"]} — done.'
    assert extract_json_object(text) == '{"met": false, "reasons": ["x"]}'


def test_extract_json_object_raises_without_object() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json_object("there is no object here")


# --------------------------------------------------------------------------- #
# parse — valid / malformed / schema-reject                                   #
# --------------------------------------------------------------------------- #
def test_parse_valid_json(tmp_path: Path) -> None:
    node, _ = _node()
    model = node.parse('{"met": true, "hollow_tests": false, "reasons": []}')
    assert model.met is True


def test_parse_malformed_json_fails_closed(tmp_path: Path) -> None:
    node, _ = _node()
    with pytest.raises(NodeOutputError):
        node.parse("{not valid json")


def test_parse_schema_violation_fails_closed(tmp_path: Path) -> None:
    node, _ = _node()
    # extra=forbid → unknown key rejected; and met missing.
    with pytest.raises(NodeOutputError):
        node.parse('{"verdict": "looks good"}')


# --------------------------------------------------------------------------- #
# invoke — re-prompt on first failure, fail-closed on the second              #
# --------------------------------------------------------------------------- #
def test_invoke_parses_valid_on_first_try(tmp_path: Path) -> None:
    node, cc = _node('{"met": true}')
    state = empty_state(tmp_path)
    model = node.invoke(state, "scope", tmp_path)
    assert model.met is True
    assert cc.calls == 1


def test_invoke_reprompts_once_then_succeeds(tmp_path: Path) -> None:
    node, cc = _node("garbage", '{"met": false, "reasons": ["nope"]}')
    state = empty_state(tmp_path)
    model = node.invoke(state, "scope", tmp_path)
    assert model.met is False
    assert cc.calls == 2
    # The re-prompt echoes the JSON schema.
    assert "schema" in cc.prompts[1].lower()
    assert "met" in cc.prompts[1]


def test_invoke_raises_when_still_bad_after_reprompt(tmp_path: Path) -> None:
    node, cc = _node("garbage", "still garbage")
    state = empty_state(tmp_path)
    with pytest.raises(NodeOutputError):
        node.invoke(state, "scope", tmp_path)
    assert cc.calls == 2  # never a third attempt — fail-closed
