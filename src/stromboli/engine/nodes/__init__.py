"""Agent nodes for the graph engine — planner, worker, verifier, reflector.

Each node is an :class:`~stromboli.engine.nodes.base.AgentNode` seam: a prompt
template (``build_prompt``), an injected Claude Code runner (``invoke``), and a
**fail-closed** typed parse (``parse``). Invalid agent output is never silently
accepted — it triggers one schema-echoing re-prompt and then raises, so the
policy treats garble as a failure rather than a pass.
"""

from __future__ import annotations

from stromboli.engine.nodes.base import AgentNode, NodeOutputError
from stromboli.engine.nodes.planner import PlannerNode
from stromboli.engine.nodes.reflector import Reflection, ReflectorNode, ReflectTarget
from stromboli.engine.nodes.schemas import FeedbackModel, PlanModel, VerdictModel
from stromboli.engine.nodes.verifier import VerifierNode, VerifyTarget
from stromboli.engine.nodes.worker import WorkerNode

__all__ = [
    "AgentNode",
    "FeedbackModel",
    "NodeOutputError",
    "PlanModel",
    "PlannerNode",
    "ReflectTarget",
    "Reflection",
    "ReflectorNode",
    "VerdictModel",
    "VerifierNode",
    "VerifyTarget",
    "WorkerNode",
]
