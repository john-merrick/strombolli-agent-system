"""Drive the Ralph loop inside a prepared worktree (US-007).

Once US-005 has a worktree and US-006 has written ``scripts/prd.json`` into it,
this module runs the build: it invokes ``claude -p`` repeatedly — each call is
one Ralph iteration that reads the repo's ``CLAUDE.md`` and works a single PRD
item — until one of three things stops it:

* the agent emits the completion signal ``<promise>COMPLETE</promise>``;
* the PRD has no **eligible** item left (every item ``passes`` or is
  ``blocked``) — checked directly so a missed signal still terminates; or
* a configurable iteration **ceiling** is hit (the loop must never spin forever).

Each iteration's stdout is captured and parsed as JSON (``claude -p
--output-format json``) so cost / token data is available to later stories
(US-008 circuit breaker, US-013 observability). ``scripts/prd.json`` and
``scripts/progress.txt`` are retained as run artifacts and surfaced on the
result.

Claude Code is invoked through an injected ``run`` callable (default: a real
``subprocess`` runner) so the loop's control flow is unit-testable without
launching a real agent.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from stromboli.breaker import BreakerTrip, CircuitBreaker
from stromboli.engine.result import UsageSpan
from stromboli.prd import PRD_RELATIVE_PATH
from stromboli.writeback import BlockedItem, read_blocked_items

logger = logging.getLogger(__name__)


def _iteration_usage(data: dict[str, Any] | None) -> tuple[int, int]:
    """Extract ``(input_tokens, output_tokens)`` from a CC JSON payload.

    Tolerates the tokens living under a ``usage`` object or at the top level;
    anything missing or non-numeric reads as zero.
    """
    if not data:
        return (0, 0)
    raw = data.get("usage")
    source: dict[str, Any] = raw if isinstance(raw, dict) else data

    def _int(key: str) -> int:
        value = source.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return (_int("input_tokens"), _int("output_tokens"))

#: The string the Ralph agent prints when no eligible item remains.
COMPLETION_SIGNAL: Final = "<promise>COMPLETE</promise>"
#: The repo's agent-instructions file, relative to the worktree root; its
#: contents are passed as the ``claude -p`` prompt for every iteration.
CLAUDE_MD_RELATIVE_PATH: Final = Path("CLAUDE.md")
#: Where the loop's progress artifact lives, relative to the worktree root.
PROGRESS_RELATIVE_PATH: Final = Path("scripts/progress.txt")
#: Default ceiling on iterations per task build, independent of per-item K.
DEFAULT_MAX_ITERATIONS: Final = 10

#: Fallback prompt when the worktree has no ``CLAUDE.md`` of its own.
_FALLBACK_PROMPT: Final = (
    "Follow the Ralph agent instructions in CLAUDE.md and work the next "
    "eligible item in scripts/prd.json."
)

#: CC runner: given the argv and the worktree cwd, run ``claude`` and return its
#: stdout, raising :class:`CCInvocationError` on a non-zero exit.
CCRunner = Callable[[Sequence[str], Path], str]


class CCInvocationError(RuntimeError):
    """A ``claude -p`` invocation exited non-zero."""


class StopReason(StrEnum):
    """Why the loop stopped — exactly one terminating condition."""

    #: The agent emitted ``<promise>COMPLETE</promise>``.
    COMPLETE = "complete"
    #: The PRD has no ``passes:false AND blocked:false`` item left.
    NO_ELIGIBLE = "no_eligible_item"
    #: The iteration ceiling was reached with work still outstanding.
    CEILING = "ceiling"
    #: A circuit-breaker ceiling (cost or iterations) tripped (US-008).
    BREAKER = "circuit_breaker"


@dataclass(frozen=True)
class Iteration:
    """One Ralph iteration: the CC invocation and its captured output."""

    index: int
    stdout: str
    data: dict[str, Any] | None
    result_text: str

    @property
    def cost_usd(self) -> float | None:
        """The iteration's USD cost from the CC JSON, if reported."""
        if self.data is None:
            return None
        value = self.data.get("total_cost_usd")
        return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class LoopResult:
    """The outcome of a loop run, plus pointers to the retained artifacts."""

    reason: StopReason
    iterations: tuple[Iteration, ...]
    prd_path: Path
    progress_path: Path
    #: The breaker trip that stopped the loop, when ``reason`` is ``BREAKER``.
    trip: BreakerTrip | None = None

    @property
    def completed(self) -> bool:
        """Whether the loop ran to a clean finish (signal or empty queue)."""
        return self.reason in (StopReason.COMPLETE, StopReason.NO_ELIGIBLE)

    @property
    def tripped(self) -> bool:
        """Whether a circuit-breaker ceiling stopped the loop."""
        return self.reason is StopReason.BREAKER

    @property
    def total_cost_usd(self) -> float:
        """Sum of per-iteration USD costs that CC reported."""
        return sum(it.cost_usd or 0.0 for it in self.iterations)

    # -- BuildResult conformance (engine Phase 0) -------------------------- #
    @property
    def artifact_path(self) -> Path:
        """The build's primary artifact — the worktree PRD (for finalize)."""
        return self.prd_path

    def blocked_items(self) -> list[BlockedItem]:
        """The PRD items the loop gave up on, with their diagnoses."""
        return read_blocked_items(self.prd_path)

    def completed_count(self) -> int:
        """Count ``passes:true`` items in the worktree PRD (for the summary)."""
        try:
            data = json.loads(self.prd_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
            return 0
        return sum(1 for item in data.get("items", []) if item.get("passes", False))

    def usage_spans(self) -> list[UsageSpan]:
        """One :class:`UsageSpan` per iteration, in order, for the tracer."""
        spans: list[UsageSpan] = []
        for it in self.iterations:
            input_tokens, output_tokens = _iteration_usage(it.data)
            spans.append(
                UsageSpan(
                    index=it.index,
                    cost_usd=it.cost_usd,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            )
        return spans


def has_eligible_items(prd_path: str | Path) -> bool:
    """Whether the PRD at ``prd_path`` has an eligible item to work.

    Eligible means ``passes`` is false **and** ``blocked`` is false; both
    default to false when the field is absent (an unworked item is eligible).
    """
    data = json.loads(Path(prd_path).read_text(encoding="utf-8"))
    items = data.get("items", [])
    return any(
        not item.get("passes", False) and not item.get("blocked", False)
        for item in items
    )


def build_cc_argv(prompt: str, *, output_format: str = "json") -> list[str]:
    """The ``claude -p`` argv for one autonomous Ralph iteration."""
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        output_format,
        "--dangerously-skip-permissions",
    ]


def _default_runner(argv: Sequence[str], cwd: Path) -> str:
    """Default :data:`CCRunner`: shell out to ``claude`` inside ``cwd``."""
    proc = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise CCInvocationError(
            f"`{' '.join(argv[:2])} …` exited {proc.returncode}: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


#: The real Claude Code runner (shell out to ``claude``); shared by both engines.
default_cc_runner: CCRunner = _default_runner


def _parse_iteration(index: int, stdout: str) -> Iteration:
    """Parse one CC stdout payload into an :class:`Iteration`.

    A malformed (non-JSON) payload is tolerated: ``data`` is ``None`` and the
    raw stdout is used as the result text so the completion signal is still
    detectable.
    """
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Iteration %d: CC output was not valid JSON", index)
        return Iteration(index=index, stdout=stdout, data=None, result_text=stdout)

    if isinstance(parsed, dict):
        result = parsed.get("result", stdout)
        result_text = result if isinstance(result, str) else stdout
        return Iteration(
            index=index, stdout=stdout, data=parsed, result_text=result_text
        )
    # A JSON array/scalar (e.g. stream-json): keep the raw text for scanning.
    return Iteration(index=index, stdout=stdout, data=None, result_text=stdout)


class RalphLoop:
    """Runs ``claude -p`` iterations against a worktree's PRD until done."""

    def __init__(
        self,
        *,
        run: CCRunner | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        output_format: str = "json",
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        self._run = run or _default_runner
        self._max_iterations = max_iterations
        self._output_format = output_format
        self._breaker = breaker

    def run(self, worktree_root: str | Path) -> LoopResult:
        """Drive the Ralph loop inside ``worktree_root`` and report the outcome.

        Stops on the completion signal, when no eligible PRD item remains, or at
        the iteration ceiling — whichever comes first. ``scripts/prd.json`` and
        ``scripts/progress.txt`` are retained and pointed at on the result.
        """
        root = Path(worktree_root)
        prd_path = root / PRD_RELATIVE_PATH
        progress_path = root / PROGRESS_RELATIVE_PATH
        prompt = self._prompt(root)

        iterations: list[Iteration] = []
        reason = StopReason.CEILING
        trip: BreakerTrip | None = None
        for index in range(1, self._max_iterations + 1):
            # Re-read the PRD each pass: the previous iteration may have flipped
            # the last item to passing/blocked, leaving nothing eligible.
            if not has_eligible_items(prd_path):
                logger.info("Loop stopped: no eligible item remains.")
                reason = StopReason.NO_ELIGIBLE
                break

            argv = build_cc_argv(prompt, output_format=self._output_format)
            logger.info("Ralph iteration %d in %s", index, root)
            stdout = self._run(argv, root)
            iteration = _parse_iteration(index, stdout)
            iterations.append(iteration)

            if COMPLETION_SIGNAL in iteration.result_text:
                logger.info("Loop stopped: completion signal at iteration %d.", index)
                reason = StopReason.COMPLETE
                break

            # A completed build is the better outcome, so the breaker is only
            # consulted once the iteration did not finish the work.
            if self._breaker is not None:
                trip = self._breaker.record_iteration(iteration.cost_usd)
                if trip is not None:
                    logger.info(
                        "Loop stopped: circuit breaker (%s) at iteration %d.",
                        trip.reason.value,
                        index,
                    )
                    reason = StopReason.BREAKER
                    break
        else:
            logger.info(
                "Loop stopped: hit the %d-iteration ceiling.", self._max_iterations
            )

        return LoopResult(
            reason=reason,
            iterations=tuple(iterations),
            prd_path=prd_path,
            progress_path=progress_path,
            trip=trip,
        )

    def _prompt(self, root: Path) -> str:
        """The ``-p`` prompt: the worktree's own ``CLAUDE.md``, else a fallback."""
        claude_md = root / CLAUDE_MD_RELATIVE_PATH
        if claude_md.exists():
            return claude_md.read_text(encoding="utf-8")
        logger.warning("No CLAUDE.md in %s; using the fallback prompt.", root)
        return _FALLBACK_PROMPT


__all__ = [
    "CLAUDE_MD_RELATIVE_PATH",
    "COMPLETION_SIGNAL",
    "DEFAULT_MAX_ITERATIONS",
    "PROGRESS_RELATIVE_PATH",
    "CCInvocationError",
    "CCRunner",
    "Iteration",
    "LoopResult",
    "RalphLoop",
    "StopReason",
    "build_cc_argv",
    "default_cc_runner",
    "has_eligible_items",
]
