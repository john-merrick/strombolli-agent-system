"""Build state for the graph engine — the pure, serializable heart.

A graph build is a small state machine over a :class:`Plan` of :class:`Unit`\\ s
(one acceptance criterion each). The control policy reads this state to pick the
next action; the orchestrator applies node outputs back onto it and persists it.
Everything here is plain data + pure helpers — no agents, no I/O beyond the
atomic disk round-trip in :meth:`BuildState.save` / :meth:`BuildState.load`.

On-disk layout (in the worktree, git-tracked) under ``.stromboli/``:

* ``state.json`` — the serialized :class:`BuildState` (atomic temp+rename);
* ``feedback.md`` — the accumulated reflector feedback, mirrored for humans; and
* ``transcripts/`` — per-node transcripts (written by the nodes later).

Each :class:`Unit` carries an explicit :class:`UnitPhase` (work → verify →
reflect) so the policy is a *pure function of state*: the orchestrator advances
the phase as nodes run, and the policy never needs to know "what just happened"
beyond what the state records.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

#: The per-build state directory, relative to the worktree root.
STATE_DIR: Final = Path(".stromboli")
#: The serialized state file, inside :data:`STATE_DIR`.
STATE_FILE: Final = "state.json"
#: The human-facing feedback mirror, inside :data:`STATE_DIR`.
FEEDBACK_FILE: Final = "feedback.md"
#: Per-node transcripts directory, inside :data:`STATE_DIR`.
TRANSCRIPTS_DIR: Final = "transcripts"

#: The verifier scope that means "the whole accumulated diff", not one unit.
WHOLE_DIFF: Final = "whole-diff"


class UnitStatus(StrEnum):
    """A unit's terminal-ish disposition in the plan."""

    #: Not yet verified — still being worked (the only *eligible* status).
    PENDING = "pending"
    #: The verifier accepted this unit's work.
    VERIFIED = "verified"
    #: Given up on (attempts exhausted / no progress) — routes to Review.
    BLOCKED = "blocked"


class UnitPhase(StrEnum):
    """Where a PENDING unit sits in the work → verify → reflect micro-cycle."""

    #: Ready for the worker to (re)write code.
    WORK = "work"
    #: Worker + objective gate passed; ready for the verifier's judgement.
    VERIFY = "verify"
    #: Verifier rejected; ready for the reflector to diagnose / feed back.
    REFLECT = "reflect"


@dataclass(frozen=True)
class Verdict:
    """The verifier's judgement of a scope (a unit id or :data:`WHOLE_DIFF`)."""

    scope: str
    met: bool
    hollow_tests: bool = False
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "met": self.met,
            "hollow_tests": self.hollow_tests,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Verdict:
        return cls(
            scope=str(data["scope"]),
            met=bool(data["met"]),
            hollow_tests=bool(data.get("hollow_tests", False)),
            reasons=tuple(data.get("reasons", ())),
        )


@dataclass
class Unit:
    """One acceptance criterion: what to build and how it's progressing."""

    id: str
    description: str
    acceptance_criterion: str
    definition_of_done: str = ""
    attempts: int = 0
    status: UnitStatus = UnitStatus.PENDING
    phase: UnitPhase = UnitPhase.WORK
    last_verdict: Verdict | None = None
    #: One diff hash per worker attempt — drives the no-progress detector.
    diff_hashes: tuple[str, ...] = ()
    #: Why the unit was blocked, set when the build parks it for Review.
    block_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "acceptance_criterion": self.acceptance_criterion,
            "definition_of_done": self.definition_of_done,
            "attempts": self.attempts,
            "status": self.status.value,
            "phase": self.phase.value,
            "last_verdict": self.last_verdict.to_dict() if self.last_verdict else None,
            "diff_hashes": list(self.diff_hashes),
            "block_reason": self.block_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Unit:
        raw_verdict = data.get("last_verdict")
        return cls(
            id=str(data["id"]),
            description=str(data.get("description", "")),
            acceptance_criterion=str(data.get("acceptance_criterion", "")),
            definition_of_done=str(data.get("definition_of_done", "")),
            attempts=int(data.get("attempts", 0)),
            status=UnitStatus(data.get("status", UnitStatus.PENDING.value)),
            phase=UnitPhase(data.get("phase", UnitPhase.WORK.value)),
            last_verdict=Verdict.from_dict(raw_verdict) if raw_verdict else None,
            diff_hashes=tuple(data.get("diff_hashes", ())),
            block_reason=str(data.get("block_reason", "")),
        )


@dataclass
class Plan:
    """A versioned set of units. ``version`` 0 means "no plan yet"."""

    version: int
    units: list[Unit] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether no plan exists yet (unversioned or unit-less)."""
        return self.version == 0 or not self.units

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "units": [u.to_dict() for u in self.units]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(
            version=int(data.get("version", 0)),
            units=[Unit.from_dict(u) for u in data.get("units", [])],
        )


@dataclass
class BuildState:
    """The whole build's state: the plan plus the cross-cutting signals the
    policy reads (replan request/count, whole-diff verdict, breaker pre-empt).
    """

    plan: Plan
    root: Path
    feedback: str = ""
    replan_count: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    #: The verifier's verdict on the whole accumulated diff, once run.
    whole_verdict: Verdict | None = None
    #: A reflector's replan request (its reason), consumed by the policy.
    pending_replan: str | None = None
    #: Set by the orchestrator when the circuit breaker pre-empts the build.
    breaker_tripped: bool = False

    # -- pure helpers ----------------------------------------------------- #
    def next_eligible_unit(self) -> Unit | None:
        """The first PENDING unit (the one the policy works next), or ``None``."""
        return next(
            (u for u in self.plan.units if u.status is UnitStatus.PENDING), None
        )

    def all_verified(self) -> bool:
        """Whether the plan has units and every one is VERIFIED."""
        return bool(self.plan.units) and all(
            u.status is UnitStatus.VERIFIED for u in self.plan.units
        )

    def blocked_units(self) -> list[Unit]:
        """The units the build gave up on (drives D10: any blocked → Review)."""
        return [u for u in self.plan.units if u.status is UnitStatus.BLOCKED]

    def record(self, step: dict[str, Any]) -> None:
        """Append one step to the run history (for resume + observability)."""
        self.history.append(step)

    def unit(self, unit_id: str) -> Unit | None:
        """Look up a unit by id."""
        return next((u for u in self.plan.units if u.id == unit_id), None)

    # -- serialization ---------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "feedback": self.feedback,
            "replan_count": self.replan_count,
            "history": self.history,
            "whole_verdict": self.whole_verdict.to_dict()
            if self.whole_verdict
            else None,
            "pending_replan": self.pending_replan,
            "breaker_tripped": self.breaker_tripped,
        }

    def save(self) -> Path:
        """Persist the state under ``root/.stromboli`` atomically.

        Writes ``state.json`` via a temp file + ``os.replace`` (so a crash never
        leaves a half-written file), mirrors ``feedback`` into ``feedback.md``,
        and ensures the ``transcripts/`` directory exists. Returns the state path.
        """
        state_dir = self.root / STATE_DIR
        (state_dir / TRANSCRIPTS_DIR).mkdir(parents=True, exist_ok=True)
        (state_dir / FEEDBACK_FILE).write_text(self.feedback, encoding="utf-8")

        target = state_dir / STATE_FILE
        tmp = state_dir / f"{STATE_FILE}.tmp"
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, root: str | Path) -> BuildState:
        """Reconstruct a :class:`BuildState` from ``root/.stromboli/state.json``."""
        root_path = Path(root)
        data = json.loads(
            (root_path / STATE_DIR / STATE_FILE).read_text(encoding="utf-8")
        )
        raw_whole = data.get("whole_verdict")
        return cls(
            plan=Plan.from_dict(data["plan"]),
            root=root_path,
            feedback=str(data.get("feedback", "")),
            replan_count=int(data.get("replan_count", 0)),
            history=list(data.get("history", [])),
            whole_verdict=Verdict.from_dict(raw_whole) if raw_whole else None,
            pending_replan=data.get("pending_replan"),
            breaker_tripped=bool(data.get("breaker_tripped", False)),
        )

    @classmethod
    def initial(cls, root: str | Path) -> BuildState:
        """A fresh, plan-less state rooted at ``root`` (version 0 → RunPlanner)."""
        return cls(plan=Plan(version=0, units=[]), root=Path(root))


__all__ = [
    "FEEDBACK_FILE",
    "STATE_DIR",
    "STATE_FILE",
    "TRANSCRIPTS_DIR",
    "WHOLE_DIFF",
    "BuildState",
    "Plan",
    "Unit",
    "UnitPhase",
    "UnitStatus",
    "Verdict",
]
