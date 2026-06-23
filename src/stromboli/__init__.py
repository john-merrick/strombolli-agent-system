"""Stromboli — agentic coding-triage worker."""

from stromboli.api import SECRET_HEADER, DispatchRequest, DispatchResponse, create_app
from stromboli.notion import NotionTaskClient, Repo, Task
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
    "SECRET_HEADER",
    "DispatchOutcome",
    "DispatchRequest",
    "DispatchResponse",
    "GitError",
    "MissingSettingsError",
    "NotionTaskClient",
    "Repo",
    "Settings",
    "Task",
    "Worker",
    "Worktree",
    "WorktreeManager",
    "clone_url",
    "create_app",
    "derive_branch_name",
    "load_settings",
    "slugify",
]
