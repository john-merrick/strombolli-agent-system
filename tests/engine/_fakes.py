"""Shared fakes for engine node tests — a scripted CC runner, no real ``claude``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from stromboli.engine.state import BuildState, Plan


def cc_envelope(agent_text: str) -> str:
    """Wrap ``agent_text`` in a ``claude -p --output-format json`` envelope."""
    return json.dumps({"type": "result", "result": agent_text})


class ScriptedCC:
    """A fake :data:`~stromboli.loop.CCRunner` returning queued outputs in order.

    Each queued string is the *agent's answer text*; it is wrapped in a CC JSON
    envelope automatically. The last item is repeated if invoked more times.
    Records every prompt passed (argv[2]) for prompt-content assertions.
    """

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.calls = 0

    def __call__(self, argv: Sequence[str], cwd: Path) -> str:
        self.prompts.append(argv[2])  # build_cc_argv → ["claude", "-p", prompt, ...]
        i = min(self.calls, len(self._answers) - 1)
        self.calls += 1
        return cc_envelope(self._answers[i])


def empty_state(root: Path) -> BuildState:
    """A plan-less state rooted at ``root`` (version 0)."""
    return BuildState(plan=Plan(version=0, units=[]), root=root)
