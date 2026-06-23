"""Tests for the in-worktree Ralph loop runner (US-007).

Claude Code is invoked through an injected runner so the loop's control flow —
how many times CC is called, the argv/cwd it is called with, and *why* the loop
stops (completion signal, no eligible item, or the iteration ceiling) — is
asserted without ever launching a real ``claude`` process. The PRD the loop
reads is a real file under ``tmp_path`` so the eligibility check exercises the
same parsing the worker uses.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from stromboli.loop import (
    COMPLETION_SIGNAL,
    CCInvocationError,
    Iteration,
    LoopResult,
    RalphLoop,
    StopReason,
    build_cc_argv,
    has_eligible_items,
)
from stromboli.prd import PRD_RELATIVE_PATH


def _prd(*items: dict[str, Any]) -> dict[str, Any]:
    return {"meta": {"maxAttempts": 3}, "items": list(items)}


def _item(
    item_id: str, *, passes: bool = False, blocked: bool = False
) -> dict[str, Any]:
    return {"id": item_id, "passes": passes, "blocked": blocked}


def _write_prd(root: Path, prd: dict[str, Any]) -> Path:
    target = root / PRD_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prd), encoding="utf-8")
    return target


def _cc_json(result_text: str, **extra: Any) -> str:
    """A single ``claude -p --output-format json`` style stdout payload."""
    return json.dumps({"type": "result", "result": result_text, **extra})


class RecordingCC:
    """A fake CC runner that records argv/cwd and replays scripted stdout.

    Each call pops the next scripted stdout (the last is repeated once the
    script is exhausted) and may run a side effect to mutate the worktree PRD,
    simulating the real agent flipping items to passing/blocked.
    """

    def __init__(
        self,
        outputs: Sequence[str],
        *,
        on_call: Sequence[Any] | None = None,
    ) -> None:
        self._outputs = list(outputs)
        self._on_call = list(on_call) if on_call is not None else []
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> str:
        index = len(self.calls)
        self.calls.append((list(argv), cwd))
        if index < len(self._on_call) and self._on_call[index] is not None:
            self._on_call[index]()
        if index < len(self._outputs):
            return self._outputs[index]
        return self._outputs[-1]

    @property
    def count(self) -> int:
        return len(self.calls)


# --------------------------------------------------------------------------- #
# Eligibility                                                                 #
# --------------------------------------------------------------------------- #


def test_has_eligible_items_true_when_a_fresh_item_remains(tmp_path: Path) -> None:
    prd_path = _write_prd(
        tmp_path, _prd(_item("a", passes=True), _item("b"))
    )
    assert has_eligible_items(prd_path) is True


def test_has_eligible_items_false_when_all_pass_or_blocked(tmp_path: Path) -> None:
    prd_path = _write_prd(
        tmp_path,
        _prd(_item("a", passes=True), _item("b", blocked=True)),
    )
    assert has_eligible_items(prd_path) is False


def test_has_eligible_items_treats_missing_fields_as_eligible(tmp_path: Path) -> None:
    # An item with neither passes nor blocked set defaults to eligible.
    prd_path = _write_prd(tmp_path, {"meta": {}, "items": [{"id": "x"}]})
    assert has_eligible_items(prd_path) is True


# --------------------------------------------------------------------------- #
# argv construction                                                           #
# --------------------------------------------------------------------------- #


def test_build_cc_argv_passes_prompt_and_json_output() -> None:
    argv = build_cc_argv("follow CLAUDE.md")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "follow CLAUDE.md" in argv
    # Per-iteration output must be machine-parseable JSON.
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--dangerously-skip-permissions" in argv


# --------------------------------------------------------------------------- #
# Loop termination                                                            #
# --------------------------------------------------------------------------- #


def test_loop_stops_on_completion_signal(tmp_path: Path) -> None:
    _write_prd(tmp_path, _prd(_item("a"), _item("b")))
    cc = RecordingCC(
        [
            _cc_json("worked item a"),
            _cc_json("worked item b"),
            _cc_json(f"all done {COMPLETION_SIGNAL}"),
            _cc_json("should never run"),
        ]
    )
    loop = RalphLoop(run=cc, max_iterations=10)

    result = loop.run(tmp_path)

    assert result.reason is StopReason.COMPLETE
    assert cc.count == 3  # stopped the moment the signal appeared
    assert len(result.iterations) == 3
    assert result.completed is True


def test_loop_stops_on_ceiling(tmp_path: Path) -> None:
    # The stub never signals completion and never flips an item, so the only
    # thing that can stop the loop is the max-iteration ceiling.
    _write_prd(tmp_path, _prd(_item("a"), _item("b")))
    cc = RecordingCC([_cc_json("still grinding")])
    loop = RalphLoop(run=cc, max_iterations=4)

    result = loop.run(tmp_path)

    assert result.reason is StopReason.CEILING
    assert cc.count == 4
    assert len(result.iterations) == 4
    assert result.completed is False


def test_loop_stops_when_no_eligible_item_remains(tmp_path: Path) -> None:
    # After the first iteration the worktree PRD has every item passing, so the
    # loop must detect "no eligible item" and stop without another CC call —
    # even though CC never emitted the completion signal.
    _write_prd(tmp_path, _prd(_item("a"), _item("b")))

    def flip_all_to_pass() -> None:
        _write_prd(
            tmp_path,
            _prd(_item("a", passes=True), _item("b", passes=True)),
        )

    cc = RecordingCC(
        [_cc_json("did the work"), _cc_json("never reached")],
        on_call=[flip_all_to_pass],
    )
    loop = RalphLoop(run=cc, max_iterations=10)

    result = loop.run(tmp_path)

    assert result.reason is StopReason.NO_ELIGIBLE
    assert cc.count == 1
    assert len(result.iterations) == 1


def test_loop_returns_no_eligible_without_invoking_cc_when_prd_already_done(
    tmp_path: Path,
) -> None:
    _write_prd(tmp_path, _prd(_item("a", passes=True)))
    cc = RecordingCC([_cc_json("noop")])
    loop = RalphLoop(run=cc, max_iterations=5)

    result = loop.run(tmp_path)

    assert result.reason is StopReason.NO_ELIGIBLE
    assert cc.count == 0
    assert result.iterations == ()


# --------------------------------------------------------------------------- #
# Invocation details & artifacts                                              #
# --------------------------------------------------------------------------- #


def test_loop_runs_cc_in_the_worktree_with_claude_md_prompt(tmp_path: Path) -> None:
    _write_prd(tmp_path, _prd(_item("a")))
    (tmp_path / "CLAUDE.md").write_text("RALPH INSTRUCTIONS HERE", encoding="utf-8")
    cc = RecordingCC([_cc_json(COMPLETION_SIGNAL)])
    loop = RalphLoop(run=cc, max_iterations=3)

    loop.run(tmp_path)

    argv, cwd = cc.calls[0]
    assert cwd == tmp_path  # CC runs *inside* the worktree
    assert "RALPH INSTRUCTIONS HERE" in argv  # the repo's CLAUDE.md is the prompt


def test_loop_captures_parsed_json_and_cost_per_iteration(tmp_path: Path) -> None:
    _write_prd(tmp_path, _prd(_item("a")))
    cc = RecordingCC(
        [_cc_json(f"done {COMPLETION_SIGNAL}", total_cost_usd=0.42, num_turns=7)]
    )
    loop = RalphLoop(run=cc, max_iterations=3)

    result = loop.run(tmp_path)

    it = result.iterations[0]
    assert isinstance(it, Iteration)
    assert it.index == 1
    assert it.data is not None
    assert it.data["num_turns"] == 7
    assert it.cost_usd == pytest.approx(0.42)
    assert COMPLETION_SIGNAL in it.result_text


def test_loop_retains_prd_and_progress_artifacts(tmp_path: Path) -> None:
    _write_prd(tmp_path, _prd(_item("a")))
    progress = tmp_path / "scripts" / "progress.txt"
    progress.write_text("run log\n", encoding="utf-8")

    def finish() -> None:
        _write_prd(tmp_path, _prd(_item("a", passes=True)))

    cc = RecordingCC([_cc_json(COMPLETION_SIGNAL)], on_call=[finish])
    loop = RalphLoop(run=cc, max_iterations=3)

    result = loop.run(tmp_path)

    # Artifacts survive the run and are reported back for downstream stories.
    assert result.prd_path.exists()
    assert result.progress_path == progress
    assert result.progress_path.exists()


def test_loop_tolerates_non_json_output(tmp_path: Path) -> None:
    # A malformed CC stdout must not crash the loop; the raw text is still
    # scanned for the completion signal.
    _write_prd(tmp_path, _prd(_item("a")))
    cc = RecordingCC([f"not json but {COMPLETION_SIGNAL}"])
    loop = RalphLoop(run=cc, max_iterations=3)

    result = loop.run(tmp_path)

    assert result.reason is StopReason.COMPLETE
    assert result.iterations[0].data is None
    assert result.iterations[0].cost_usd is None


def test_loop_propagates_cc_invocation_errors(tmp_path: Path) -> None:
    _write_prd(tmp_path, _prd(_item("a")))

    def boom(argv: Sequence[str], cwd: Path) -> str:
        raise CCInvocationError("claude exited 1")

    loop = RalphLoop(run=boom, max_iterations=3)

    with pytest.raises(CCInvocationError):
        loop.run(tmp_path)


def test_loop_result_is_immutable() -> None:
    result = LoopResult(
        reason=StopReason.COMPLETE,
        iterations=(),
        prd_path=Path("scripts/prd.json"),
        progress_path=Path("scripts/progress.txt"),
    )
    with pytest.raises((AttributeError, TypeError)):
        result.reason = StopReason.CEILING  # type: ignore[misc]
