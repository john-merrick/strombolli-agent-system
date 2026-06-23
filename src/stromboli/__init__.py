"""Stromboli — agentic coding-triage worker."""

from stromboli.api import SECRET_HEADER, DispatchRequest, DispatchResponse, create_app
from stromboli.loop import (
    COMPLETION_SIGNAL,
    CCInvocationError,
    Iteration,
    LoopResult,
    RalphLoop,
    StopReason,
    has_eligible_items,
)
from stromboli.notion import NotionTaskClient, Repo, Task
from stromboli.prd import (
    build_prd,
    parse_acceptance_criteria,
    write_prd,
)
from stromboli.settings import MissingSettingsError, Settings, load_settings
from stromboli.worker import ASSIGNEE_AGENT, DispatchOutcome, Worker
from stromboli.worktree import (
    GitError,
    Worktree,
    WorktreeManager,
    clone_url,
    derive_branch_name,
    slugify,
)

__all__ = [
    "ASSIGNEE_AGENT",
    "COMPLETION_SIGNAL",
    "SECRET_HEADER",
    "CCInvocationError",
    "DispatchOutcome",
    "DispatchRequest",
    "DispatchResponse",
    "GitError",
    "Iteration",
    "LoopResult",
    "MissingSettingsError",
    "NotionTaskClient",
    "RalphLoop",
    "Repo",
    "Settings",
    "StopReason",
    "Task",
    "Worker",
    "Worktree",
    "WorktreeManager",
    "build_prd",
    "clone_url",
    "create_app",
    "derive_branch_name",
    "has_eligible_items",
    "load_settings",
    "parse_acceptance_criteria",
    "slugify",
    "write_prd",
]
