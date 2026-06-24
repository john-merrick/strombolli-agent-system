"""Tests for the objective gate (Phase 3).

The gate runs the repo's checks in order and reports pass / check-failure /
tooling-crash. A fake command runner drives every branch — no subprocess, no
real pytest/ruff/mypy.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from stromboli.engine.gate import (
    DEFAULT_COMMANDS,
    GateCommand,
    ObjectiveGate,
)


class FakeRunner:
    """Returns a queued ``(code, output)`` per call, recording the argvs run."""

    def __init__(self, *results: tuple[int, str]) -> None:
        self._results = list(results)
        self.ran: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        self.ran.append(tuple(argv))
        return self._results[len(self.ran) - 1]


CMDS = (
    GateCommand(("pytest",), crash_codes=frozenset({2, 3, 4, 5})),
    GateCommand(("ruff",)),
    GateCommand(("mypy",), crash_codes=frozenset({2})),
)


def test_all_pass(tmp_path: Path) -> None:
    runner = FakeRunner((0, ""), (0, ""), (0, ""))
    result = ObjectiveGate(CMDS, run=runner).run(tmp_path)
    assert result.passed is True
    assert result.crashed is False
    # Every command ran, in order.
    assert runner.ran == [("pytest",), ("ruff",), ("mypy",)]


def test_check_failure_is_not_a_crash(tmp_path: Path) -> None:
    # pytest exits 1 → tests failed (ran fine), not a tooling crash.
    runner = FakeRunner((1, "1 failed"))
    result = ObjectiveGate(CMDS, run=runner).run(tmp_path)
    assert result.passed is False
    assert result.crashed is False
    assert result.command == ("pytest",)
    assert "1 failed" in result.output


def test_tooling_crash_is_flagged(tmp_path: Path) -> None:
    # pytest exits 4 (usage error) → crash, not a check failure.
    runner = FakeRunner((4, "usage error"))
    result = ObjectiveGate(CMDS, run=runner).run(tmp_path)
    assert result.passed is False
    assert result.crashed is True
    assert result.command == ("pytest",)


def test_short_circuits_on_first_non_pass(tmp_path: Path) -> None:
    # pytest passes, ruff fails → mypy never runs.
    runner = FakeRunner((0, ""), (1, "lint error"))
    result = ObjectiveGate(CMDS, run=runner).run(tmp_path)
    assert result.passed is False
    assert result.command == ("ruff",)
    assert runner.ran == [("pytest",), ("ruff",)]


def test_runner_exception_is_a_crash(tmp_path: Path) -> None:
    def boom(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        raise FileNotFoundError("pytest: command not found")

    result = ObjectiveGate(CMDS, run=boom).run(tmp_path)
    assert result.passed is False
    assert result.crashed is True
    assert "command not found" in result.output


def test_non_crash_code_for_command_without_crash_codes(tmp_path: Path) -> None:
    # ruff has no crash codes → even exit 2 is a plain failure here.
    runner = FakeRunner((0, ""), (2, "ruff internal"))
    result = ObjectiveGate(CMDS, run=runner).run(tmp_path)
    assert result.command == ("ruff",)
    assert result.crashed is False


def test_empty_command_list_rejected() -> None:
    with pytest.raises(ValueError, match="at least one command"):
        ObjectiveGate([])


def test_default_commands_cover_pytest_ruff_mypy() -> None:
    argvs = [c.argv for c in DEFAULT_COMMANDS]
    assert ("uv", "run", "pytest", "-q") in argvs
    assert any("ruff" in a for a in argvs)
    assert any("mypy" in a for a in argvs)
    # pytest's collection/usage exits are treated as crashes.
    pytest_cmd = next(c for c in DEFAULT_COMMANDS if "pytest" in c.argv)
    assert {2, 3, 4, 5} <= pytest_cmd.crash_codes
