"""Tests for the read-only Investigator agent (the investigate loop, Phase 3)."""

from __future__ import annotations

from pathlib import Path

from stromboli.llm.gateway import GatewayError
from stromboli.llm.investigator import Investigator
from stromboli.orchestration.paused import PausedIndex, PausedTask
from stromboli.state import Spec, StromboliState, TestResult, Verdict
from tests.nodes._fakes import FakeGateway


def _suspend(idx: PausedIndex) -> PausedTask:
    state = StromboliState(
        task_id="a", source="notion", raw_request="x",
        spec=Spec(goal="add flag"),
        verdict=Verdict(decision="revise", reason="missing a test for the flag"),
        code_diff="diff --git a/foo.py b/foo.py\n+flag = True",
        test_results=[TestResult(passed=False, summary="1 failed", raw="E assert")],
    )
    task = idx.suspend(state, reason="verifier revise", name="add flag")
    idx.append_message("a", "human", "why did it fail?")
    return task


def test_investigator_returns_message_and_guidance(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    task = _suspend(idx)
    gw = FakeGateway(
        {"message": "It's missing a test.", "guidance": "Add test_flag in tests/."}
    )
    inv = Investigator(gateway=gw, model="m", index=idx)
    message, guidance = inv.respond(task, "why did it fail?")
    assert "missing a test" in message.lower()
    assert guidance == "Add test_flag in tests/."
    # The verdict + diff were fed to the model as read-only context.
    fed = gw.calls[0]["user"]
    assert "missing a test for the flag" in fed and "diff --git" in fed


def test_investigator_empty_guidance_is_none(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    task = _suspend(idx)
    gw = FakeGateway({"message": "What error do you see?", "guidance": ""})
    inv = Investigator(gateway=gw, model="m", index=idx)
    _message, guidance = inv.respond(task, "help")
    assert guidance is None


def test_investigator_gateway_error_is_graceful(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    task = _suspend(idx)
    gw = FakeGateway(error=GatewayError("gateway down"))
    inv = Investigator(gateway=gw, model="m", index=idx)
    message, guidance = inv.respond(task, "help")
    assert "couldn't analyze" in message.lower() and guidance is None


def test_investigator_caps_long_threads(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    task = _suspend(idx)  # one human message already
    idx.append_message("a", "human", "second")
    gw = FakeGateway({"message": "x", "guidance": "y"})
    inv = Investigator(gateway=gw, model="m", index=idx, max_turns=1)
    message, guidance = inv.respond(task, "second")
    assert "✅" in message and guidance is None
    assert gw.calls == []  # the model was never called past the cap
