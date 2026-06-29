"""Stromboli — an explicit graph state machine for agentic coding triage.

A task captured in Notion is specced, coded (the Claude Agent SDK's agent loop),
verified by an independent non-Claude model, and opened as a PR — orchestrated by
a LangGraph ``StateGraph`` with per-node observability and a three-tier memory.
"""

from stromboli.config import Budgets, Config, Models, from_settings
from stromboli.graph import GraphDeps, build_graph, run_task
from stromboli.settings import MissingSettingsError, Settings, load_settings
from stromboli.state import (
    Spec,
    StromboliState,
    TestResult,
    Verdict,
)

__all__ = [
    "Budgets",
    "Config",
    "GraphDeps",
    "MissingSettingsError",
    "Models",
    "Settings",
    "Spec",
    "StromboliState",
    "TestResult",
    "Verdict",
    "build_graph",
    "from_settings",
    "load_settings",
    "run_task",
]
