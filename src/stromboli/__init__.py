"""Stromboli — agentic coding-triage worker."""

from stromboli.api import SECRET_HEADER, DispatchRequest, DispatchResponse, create_app
from stromboli.app import build_deps, create_stromboli_app
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
from stromboli.observability import (
    BuildTracer,
    LangfuseTracer,
    NullTracer,
    build_tracer,
    record_build_trace,
)
from stromboli.pipeline import BuildDeps, run_build
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
from stromboli.routing import RouteOutcome, decide_route, route_task
from stromboli.settings import MissingSettingsError, Settings, load_settings
from stromboli.worker import ASSIGNEE_AGENT, ASSIGNEE_HUMAN, DispatchOutcome, Worker
from stromboli.worktree import (
    GitError,
    Worktree,
    WorktreeManager,
    clone_url,
    derive_branch_name,
    slugify,
)
from stromboli.writeback import (
    BlockedItem,
    build_feedback_summary,
    read_blocked_items,
    resilient_append,
)

__all__ = [
    "ASSIGNEE_AGENT",
    "ASSIGNEE_HUMAN",
    "COMPLETION_SIGNAL",
    "SECRET_HEADER",
    "BlockedItem",
    "BreakerConfig",
    "BreakerTrip",
    "BuildDeps",
    "BuildTracer",
    "CCInvocationError",
    "CircuitBreaker",
    "DispatchOutcome",
    "DispatchRequest",
    "DispatchResponse",
    "GitError",
    "GitHubClient",
    "Iteration",
    "LangfuseTracer",
    "LoopResult",
    "MissingSettingsError",
    "NotionTaskClient",
    "NullTracer",
    "PublishResult",
    "PullRequest",
    "RalphLoop",
    "Repo",
    "RouteOutcome",
    "Settings",
    "StopReason",
    "Task",
    "TripReason",
    "Worker",
    "Worktree",
    "WorktreeManager",
    "build_deps",
    "build_feedback_summary",
    "build_prd",
    "build_tracer",
    "clone_url",
    "create_app",
    "create_stromboli_app",
    "decide_route",
    "derive_branch_name",
    "derive_pr_body",
    "derive_pr_title",
    "handle_trip",
    "has_eligible_items",
    "load_settings",
    "parse_acceptance_criteria",
    "publish_pr",
    "read_blocked_items",
    "record_build_trace",
    "resilient_append",
    "route_task",
    "run_build",
    "slugify",
    "write_prd",
]
