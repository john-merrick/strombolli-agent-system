"""The agent-node seam: build_prompt → invoke (injected CC) → parse (fail-closed).

Every typed node (planner / verifier / reflector) is an :class:`AgentNode`: it
builds a prompt from state, invokes Claude Code through the *injected*
:data:`~stromboli.loop.CCRunner` (so tests use a fake — never a real ``claude``),
and parses the agent's JSON into a strict pydantic model.

The parse is **fail-closed**, which is the whole point of the seam: malformed
JSON or a schema violation does not become a silent pass. Instead the node
re-prompts *once* with the JSON schema echoed back, and if the second attempt is
still bad it raises :class:`NodeOutputError` — which the policy treats as a node
failure (the unit is reflected / eventually blocked), never as acceptance.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from stromboli.engine.state import BuildState
from stromboli.loop import CCRunner, build_cc_argv

logger = logging.getLogger(__name__)

TargetT = TypeVar("TargetT")
ModelT = TypeVar("ModelT", bound=BaseModel)

#: A fenced ```json … ``` code block leader, stripped before JSON extraction.
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*")


class NodeOutputError(RuntimeError):
    """An agent's output could not be parsed into its schema, even after a
    re-prompt. The policy treats this as a node failure — never a pass."""


def result_text(stdout: str) -> str:
    """Extract the agent's answer text from a ``claude -p --output-format json``
    envelope. Tolerant: a non-JSON or non-dict payload is returned verbatim so
    the downstream JSON extraction still gets a chance.
    """
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout
    if isinstance(parsed, dict):
        result = parsed.get("result")
        return result if isinstance(result, str) else stdout
    return stdout


def extract_json_object(text: str) -> str:
    """Return the JSON object embedded in ``text`` (tolerating fences / prose).

    Raises :class:`ValueError` when no ``{…}`` object is present, so the caller
    can fail closed rather than guess.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped)
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in agent output")
    return stripped[start : end + 1]


class AgentNode(Generic[TargetT, ModelT]):
    """A prompt template + injected CC runner + fail-closed typed parse."""

    def __init__(
        self, run: CCRunner, model_cls: type[ModelT], *, name: str
    ) -> None:
        self._run = run
        self._model_cls = model_cls
        self.name = name

    # -- to be overridden ------------------------------------------------- #
    def build_prompt(self, state: BuildState, target: TargetT) -> str:
        """Render the agent prompt for ``target`` in ``state``."""
        raise NotImplementedError

    # -- the seam --------------------------------------------------------- #
    def parse(self, text: str) -> ModelT:
        """Parse ``text`` into the node's model, or raise :class:`NodeOutputError`."""
        try:
            payload = extract_json_object(text)
            return self._model_cls.model_validate_json(payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise NodeOutputError(
                f"{self.name}: could not parse output into {self._model_cls.__name__}: {exc}"
            ) from exc

    def invoke(self, state: BuildState, target: TargetT, cwd: Path) -> ModelT:
        """Run the node end-to-end: prompt → CC → parse, with one re-prompt.

        On a first-attempt parse failure the prompt is reissued with the JSON
        schema echoed; a second failure raises :class:`NodeOutputError`.
        """
        prompt = self.build_prompt(state, target)
        text = self._run_cc(prompt, cwd)
        try:
            return self.parse(text)
        except NodeOutputError as first:
            logger.warning(
                "%s: bad output, re-prompting with schema. (%s)", self.name, first
            )
            retry = self._reprompt(prompt, text)
            text2 = self._run_cc(retry, cwd)
            return self.parse(text2)  # fail-closed: raises if still bad

    # -- internals -------------------------------------------------------- #
    def _run_cc(self, prompt: str, cwd: Path) -> str:
        stdout = self._run(build_cc_argv(prompt), cwd)
        return result_text(stdout)

    def _reprompt(self, prompt: str, bad_output: str) -> str:
        schema = json.dumps(self._model_cls.model_json_schema(), indent=2)
        return (
            f"{prompt}\n\n"
            "Your previous response could not be parsed:\n"
            f"{bad_output.strip()[:500]}\n\n"
            "Reply with ONLY a single JSON object that validates against this "
            "schema (no prose, no code fences):\n"
            f"{schema}"
        )


__all__ = [
    "AgentNode",
    "NodeOutputError",
    "extract_json_object",
    "result_text",
]
