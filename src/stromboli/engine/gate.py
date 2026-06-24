"""The objective gate: run the repo's test / lint / type commands (Phase 3).

Where the verifier is an *agent* judging acceptance, the gate is *objective
code*: it runs the target repo's real checks in the worktree and reports a
binary :class:`GateResult`. The orchestrator runs it after each worker attempt;
a fail sends the unit back to the worker.

Critically (design §5.4) the gate distinguishes a **check failure** (the tool
ran and found problems — e.g. a failing test) from a **tooling crash** (the tool
itself could not run — bad invocation, missing binary, internal error). They are
both "not passed", but the orchestrator treats a crash as an infrastructure
problem to surface, not as "the worker's code is wrong" — so it must not be fed
back as if the tests merely failed.

Commands are **configurable per target repo** (some repos use ``make test`` or a
different type checker); the default is ``uv run pytest -q`` / ``ruff check`` /
``mypy``. The command runner is injected so the gate is unit-tested with a fake.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: A command runner: given argv + cwd, return ``(returncode, combined_output)``.
CommandRunner = Callable[[Sequence[str], Path], "tuple[int, str]"]


@dataclass(frozen=True)
class GateCommand:
    """One objective check: its argv and which exit codes mean a *crash*.

    ``crash_codes`` are non-zero exits that indicate the tool failed to run
    rather than found problems (e.g. pytest's 2–5: interrupted / internal error /
    usage error / no tests collected). Any other non-zero exit is a check
    failure (the tool ran and the code is wrong).
    """

    argv: tuple[str, ...]
    crash_codes: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class GateResult:
    """The gate's verdict. ``passed`` ⇒ every command exited 0."""

    passed: bool
    output: str = ""
    crashed: bool = False
    #: The argv of the first command that failed or crashed, if any.
    command: tuple[str, ...] | None = None


#: The default per-repo checks. pytest's 2–5 are tooling crashes, not failures.
DEFAULT_COMMANDS: tuple[GateCommand, ...] = (
    GateCommand(("uv", "run", "pytest", "-q"), crash_codes=frozenset({2, 3, 4, 5})),
    GateCommand(("uv", "run", "ruff", "check", "."), crash_codes=frozenset({2})),
    GateCommand(("uv", "run", "mypy"), crash_codes=frozenset({2})),
)


def _default_runner(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Default :data:`CommandRunner`: run the command, capture combined output."""
    proc = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, capture_output=True, text=True
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class ObjectiveGate:
    """Runs the configured checks in order, short-circuiting on the first non-pass."""

    def __init__(
        self,
        commands: Sequence[GateCommand] = DEFAULT_COMMANDS,
        run: CommandRunner | None = None,
    ) -> None:
        if not commands:
            raise ValueError("ObjectiveGate needs at least one command")
        self._commands = tuple(commands)
        self._run = run or _default_runner

    def run(self, cwd: str | Path) -> GateResult:
        """Run every check in ``cwd``; return the first non-pass, else passed."""
        root = Path(cwd)
        for cmd in self._commands:
            try:
                code, output = self._run(cmd.argv, root)
            except Exception as exc:  # noqa: BLE001 - a runner blow-up is a crash
                logger.warning("Gate command %s could not run: %s", cmd.argv, exc)
                return GateResult(
                    passed=False, output=str(exc), crashed=True, command=cmd.argv
                )
            if code == 0:
                continue
            crashed = code in cmd.crash_codes
            logger.info(
                "Gate %s for %s (exit %d)",
                "crashed" if crashed else "failed",
                " ".join(cmd.argv),
                code,
            )
            return GateResult(
                passed=False, output=output, crashed=crashed, command=cmd.argv
            )
        return GateResult(passed=True)


__all__ = [
    "DEFAULT_COMMANDS",
    "CommandRunner",
    "GateCommand",
    "GateResult",
    "ObjectiveGate",
]
