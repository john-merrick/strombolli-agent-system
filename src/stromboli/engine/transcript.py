"""Per-step transcripts — the build's black box on disk.

The orchestrator records one :class:`StepRecord` per graph step into
``.stromboli/transcripts/NNN-<action>.md``: the exact prompt(s) each agent saw,
its raw output, the objective-gate command + result (for worker steps), and a
one-line outcome. Because the file lives in the worktree, it travels with the PR
a human reviews — so debugging a build means reading its transcripts top to
bottom, not reconstructing what an agent saw from logs that have scrolled away.

This module is pure rendering + a single disk write, so it is unit-tested
without the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from stromboli.engine.state import STATE_DIR, TRANSCRIPTS_DIR


@dataclass(frozen=True)
class Exchange:
    """One agent round-trip: the prompt sent and the raw output received."""

    prompt: str
    raw_output: str


@dataclass(frozen=True)
class StepRecord:
    """Everything that happened in one graph step, for the transcript file."""

    step: int
    action: str
    outcome: str = ""
    exchanges: tuple[Exchange, ...] = ()
    #: The objective-gate command + result, when this step ran the gate.
    gate: str | None = None

    @property
    def filename(self) -> str:
        """The transcript file name, zero-padded and ordered by step."""
        return f"{self.step:03d}-{self.action}.md"


def render_transcript(record: StepRecord) -> str:
    """Render a :class:`StepRecord` as a readable markdown transcript."""
    lines: list[str] = [f"# Step {record.step:03d} — {record.action}", ""]

    lines += ["## Outcome", "", record.outcome or "(none recorded)", ""]

    if record.gate is not None:
        lines += ["## Objective gate", "", record.gate, ""]

    if not record.exchanges:
        lines += ["## Agent exchanges", "", "(no agent call in this step)", ""]
    for i, exchange in enumerate(record.exchanges, start=1):
        lines += [
            f"## Exchange {i} — prompt",
            "",
            exchange.prompt,
            "",
            f"## Exchange {i} — raw output",
            "",
            exchange.raw_output,
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"


def write_transcript(root: str | Path, record: StepRecord) -> Path:
    """Write ``record`` to ``root/.stromboli/transcripts/NNN-<action>.md``."""
    transcripts = Path(root) / STATE_DIR / TRANSCRIPTS_DIR
    transcripts.mkdir(parents=True, exist_ok=True)
    target = transcripts / record.filename
    target.write_text(render_transcript(record), encoding="utf-8")
    return target


@dataclass
class TranscriptRecorder:
    """Accumulates the agent exchanges seen during a single step.

    The orchestrator's metered CC runner feeds it each ``(prompt, raw_output)``
    as agents are invoked; the orchestrator then drains it into a
    :class:`StepRecord` once per step (a re-prompt produces two exchanges).
    """

    _pending: list[Exchange] = field(default_factory=list)

    def add(self, prompt: str, raw_output: str) -> None:
        self._pending.append(Exchange(prompt=prompt, raw_output=raw_output))

    def drain(self) -> tuple[Exchange, ...]:
        """Return the exchanges since the last drain and reset."""
        exchanges = tuple(self._pending)
        self._pending.clear()
        return exchanges


__all__ = [
    "Exchange",
    "StepRecord",
    "TranscriptRecorder",
    "render_transcript",
    "write_transcript",
]
