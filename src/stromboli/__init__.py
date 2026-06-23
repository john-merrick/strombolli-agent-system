"""Stromboli — agentic coding-triage worker."""

from stromboli.api import SECRET_HEADER, DispatchRequest, DispatchResponse, create_app
from stromboli.breaker import (
    BreakerConfig,
    BreakerTrip,
    CircuitBreaker,
    TripReason,
    handle_trip,
)
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
from stromboli.pr import (
    GitHubClient,
    PublishResult,
    PullRequest,
    derive_pr_body,
    derive_pr_title,
    publish_pr,
)
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
    "BreakerConfig",
    "BreakerTrip",
    "CCInvocationError",
    "CircuitBreaker",
    "DispatchOutcome",
    "DispatchRequest",
    "DispatchResponse",
    "GitError",
    "GitHubClient",
    "Iteration",
    "LoopResult",
    "MissingSettingsError",
    "NotionTaskClient",
    "PublishResult",
    "PullRequest",
    "RalphLoop",
    "Repo",
    "Settings",
    "StopReason",
    "Task",
    "TripReason",
    "Worker",
    "Worktree",
    "WorktreeManager",
    "build_prd",
    "clone_url",
    "create_app",
    "derive_branch_name",
    "derive_pr_body",
    "derive_pr_title",
    "handle_trip",
    "has_eligible_items",
    "load_settings",
    "parse_acceptance_criteria",
    "publish_pr",
    "slugify",
    "write_prd",
]
