"""A typed Notion task client for the Stromboli worker.

The client reads a task page, resolves its target GitHub repository from the
related Project page, and writes status / PR / cost / token updates plus a
markdown feedback summary back to the task.

Parsing is kept as a set of *pure* functions (:func:`parse_task`,
:func:`parse_repo`) so the field extraction and repo resolution can be tested
directly against mocked Notion API payloads without any network access. The
:class:`NotionTaskClient` is a thin HTTP shell over those parsers.

Status writes map onto the **exact existing** Notion ``status`` option labels
(``To do`` / ``Working on`` / ``Review`` / ``Complete``). Notion's API does not
auto-create ``status`` options, so sending an unknown name would error; we guard
against that up front by validating against :data:`VALID_STATUSES`.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

logger = logging.getLogger(__name__)

#: The assignee label that marks a task as the agent's to build (intake guard).
ASSIGNEE_AGENT: Final = "Agent"
#: Base seconds for exponential backoff between write-back retries.
_BACKOFF_BASE: Final = 0.5

# --------------------------------------------------------------------------- #
# Status labels — these MUST match the Notion Status property options exactly. #
# --------------------------------------------------------------------------- #

STATUS_TODO: Final = "To do"
STATUS_WORKING: Final = "Working on"
STATUS_REVIEW: Final = "Review"
STATUS_COMPLETE: Final = "Complete"

VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_TODO, STATUS_WORKING, STATUS_REVIEW, STATUS_COMPLETE}
)

#: Notion property names on the task page (must match the database schema).
PROP_TASK_NAME: Final = "Task name"
PROP_PROJECT: Final = "Project"
PROP_STATUS: Final = "Status"
PROP_SPEC: Final = "Spec"
PROP_ASSIGNED_TO: Final = "Assigned to"
PROP_READY: Final = "Ready"
PROP_NEEDS_REVIEW: Final = "Needs review"
PROP_PR: Final = "PR"
PROP_COST: Final = "Cost"
PROP_TOKENS: Final = "Tokens"
#: Property name on the related Project page holding the GitHub repo.
PROP_REPO: Final = "Repo"


# Sentinel distinguishing "argument not provided" from "set to None/0".
class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<unset>"


_UNSET: Final = _Unset()


@dataclass(frozen=True)
class Repo:
    """A resolved GitHub repository (``owner/repo``)."""

    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class Task:
    """A typed view of a Stromboli task page."""

    page_id: str
    name: str
    project_ids: tuple[str, ...]
    status: str | None
    spec: str
    assigned_to: str | None
    ready: bool
    needs_review: bool
    pr_url: str | None
    cost: float | None
    tokens: int | None


# --------------------------------------------------------------------------- #
# Pure property extractors                                                     #
# --------------------------------------------------------------------------- #


def _rich_text(prop: dict[str, Any], key: str) -> str:
    """Concatenate the ``plain_text`` of a rich_text / title property."""
    segments = prop.get(key) or []
    return "".join(segment.get("plain_text", "") for segment in segments)


def _checkbox(props: dict[str, Any], name: str) -> bool:
    prop = props.get(name) or {}
    return bool(prop.get("checkbox", False))


def _number(props: dict[str, Any], name: str) -> float | None:
    prop = props.get(name) or {}
    value = prop.get("number")
    return None if value is None else float(value)


def _select_name(prop: dict[str, Any]) -> str | None:
    """Read a name from a ``select`` or ``people`` property."""
    select = prop.get("select")
    if select:
        name = select.get("name")
        return None if name is None else str(name)
    people = prop.get("people") or []
    if people:
        name = people[0].get("name")
        return None if name is None else str(name)
    return None


def parse_task(page: dict[str, Any]) -> Task:
    """Parse a Notion *page* object into a :class:`Task`.

    Missing or empty fields degrade to sensible empties (``""`` / ``None`` /
    ``False``) rather than raising, so partially-filled task rows still parse.
    """
    props: dict[str, Any] = page.get("properties", {})

    # The "Status" property may be a Notion select OR the dedicated status type;
    # tolerate both so the field parses regardless of how the DB was defined.
    status_prop = props.get(PROP_STATUS) or {}
    status_value = status_prop.get("select") or status_prop.get("status")
    status = status_value.get("name") if status_value else None

    relation = (props.get(PROP_PROJECT) or {}).get("relation") or []
    project_ids = tuple(rel["id"] for rel in relation if rel.get("id"))

    tokens = _number(props, PROP_TOKENS)

    return Task(
        page_id=page.get("id", ""),
        name=_rich_text(props.get(PROP_TASK_NAME) or {}, "title"),
        project_ids=project_ids,
        status=status,
        spec=_rich_text(props.get(PROP_SPEC) or {}, "rich_text"),
        assigned_to=_select_name(props.get(PROP_ASSIGNED_TO) or {}),
        ready=_checkbox(props, PROP_READY),
        needs_review=_checkbox(props, PROP_NEEDS_REVIEW),
        pr_url=(props.get(PROP_PR) or {}).get("url"),
        cost=_number(props, PROP_COST),
        tokens=None if tokens is None else int(tokens),
    )


_GITHUB_RE: Final = re.compile(
    r"""
    (?:https?://github\.com/ | git@github\.com: )?  # optional URL / SSH prefix
    (?P<owner>[A-Za-z0-9._-]+) / (?P<repo>[A-Za-z0-9._-]+?)
    (?:\.git)? /? $                                  # optional .git / trailing /
    """,
    re.VERBOSE,
)


def parse_repo(project_page: dict[str, Any]) -> Repo:
    """Resolve ``owner/repo`` from a related Project page's ``Repo`` property.

    Accepts a GitHub HTTPS URL, an SSH URL, or a bare ``owner/repo`` string
    (stored as a ``url`` or ``rich_text`` property).
    """
    props: dict[str, Any] = project_page.get("properties", {})
    prop = props.get(PROP_REPO)
    if not prop:
        raise ValueError(f"Project page has no '{PROP_REPO}' property")

    raw = prop.get("url") if prop.get("type") == "url" else None
    if not raw:
        raw = _rich_text(prop, "rich_text") or prop.get("url")
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"Project '{PROP_REPO}' property is empty")

    match = _GITHUB_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"Cannot resolve owner/repo from {raw!r}")
    return Repo(owner=match.group("owner"), repo=match.group("repo"))


# --------------------------------------------------------------------------- #
# HTTP client                                                                 #
# --------------------------------------------------------------------------- #


def _markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert markdown into Notion paragraph blocks (one per blank-line group).

    A deliberately minimal converter for v1: each non-empty block of text
    (paragraphs separated by blank lines) becomes a ``paragraph`` block.
    """
    blocks: list[dict[str, Any]] = []
    for chunk in re.split(r"\n\s*\n", markdown):
        text = chunk.strip()
        if not text:
            continue
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                },
            }
        )
    return blocks


class NotionTaskClient:
    """Typed HTTP client for Stromboli task pages."""

    BASE_URL: Final = "https://api.notion.com"
    NOTION_VERSION: Final = "2022-06-28"

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        # A generous timeout — Notion can be slow, and a short default (5s) makes
        # the autonomous poller flake with ReadTimeouts.
        self._client = client or httpx.Client(
            base_url=self.BASE_URL, timeout=30.0
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self.NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # -- reads --------------------------------------------------------------- #

    def _get_page(self, page_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/v1/pages/{page_id}", headers=self._headers)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def get_task(self, page_id: str) -> Task:
        """Fetch and parse a task page."""
        return parse_task(self._get_page(page_id))

    def get_project_repo(self, task: Task) -> Repo:
        """Resolve the target repo from the task's related Project page."""
        if not task.project_ids:
            raise ValueError(f"Task {task.page_id} has no Project relation")
        return parse_repo(self._get_page(task.project_ids[0]))

    def query_ready_tasks(self, database_id: str) -> list[Task]:
        """Return the tasks in ``database_id`` that are ready for the agent.

        The intake guard (PRD §6.1): ``Ready`` is checked **and** ``Assigned to``
        is ``Agent`` **and** ``Status`` is ``To do``. The ``Ready`` checkbox is
        pushed into the Notion query filter; the assignee/status guard is applied
        in Python (after :func:`parse_task`) so it tolerates either Notion status
        property shape. This is the front-end for adding tasks to the workflow.
        """
        resp = self._client.post(
            f"/v1/databases/{database_id}/query",
            headers=self._headers,
            json={"filter": {"property": PROP_READY, "checkbox": {"equals": True}}},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        tasks = [parse_task(page) for page in data.get("results", [])]
        return [t for t in tasks if is_dispatchable(t)]

    # -- writes -------------------------------------------------------------- #

    def update_task(
        self,
        page_id: str,
        *,
        status: str | None = None,
        pr_url: str | None | _Unset = _UNSET,
        cost: float | None | _Unset = _UNSET,
        tokens: int | None | _Unset = _UNSET,
    ) -> None:
        """Write Status / PR / Cost / Tokens; only provided fields are sent.

        ``status`` is validated against the exact existing Notion option labels
        (:data:`VALID_STATUSES`) so we never ask the API to create an option.
        """
        properties: dict[str, Any] = {}

        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(
                    f"Unknown status {status!r}; must be one of "
                    f"{sorted(VALID_STATUSES)}"
                )
            # The tasks DB defines "Status" as a select property.
            properties[PROP_STATUS] = {"select": {"name": status}}
        if not isinstance(pr_url, _Unset):
            properties[PROP_PR] = {"url": pr_url}
        if not isinstance(cost, _Unset):
            properties[PROP_COST] = {"number": cost}
        if not isinstance(tokens, _Unset):
            properties[PROP_TOKENS] = {"number": tokens}

        if not properties:
            return

        resp = self._client.patch(
            f"/v1/pages/{page_id}",
            headers=self._headers,
            json={"properties": properties},
        )
        resp.raise_for_status()

    def set_rich_text(self, page_id: str, prop_name: str, text: str) -> None:
        """Write ``text`` into a rich_text property (e.g. the ``Prompt`` field).

        Notion caps a rich_text segment at 2000 chars, so the value is chunked.
        """
        chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)] or [""]
        rich_text = [{"type": "text", "text": {"content": c}} for c in chunks]
        resp = self._client.patch(
            f"/v1/pages/{page_id}",
            headers=self._headers,
            json={"properties": {prop_name: {"rich_text": rich_text}}},
        )
        resp.raise_for_status()

    def append_task_body(self, page_id: str, markdown: str) -> None:
        """Append markdown to the task page body as paragraph blocks."""
        children = _markdown_to_blocks(markdown)
        if not children:
            return
        resp = self._client.patch(
            f"/v1/blocks/{page_id}/children",
            headers=self._headers,
            json={"children": children},
        )
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Intake guard + resilient write-back (folded from the removed writeback.py)   #
# --------------------------------------------------------------------------- #


def is_dispatchable(task: Task) -> bool:
    """The intake guard: Ready, assigned to the Agent, and still ``To do``."""
    return (
        task.ready
        and task.assigned_to == ASSIGNEE_AGENT
        and task.status == STATUS_TODO
    )


class AppendGateway(Protocol):
    """The slice of :class:`NotionTaskClient` the write-back helpers need."""

    def append_task_body(self, page_id: str, markdown: str) -> None: ...


def build_feedback_summary(
    *,
    status: str,
    pr_url: str | None,
    reflections: Sequence[str] = (),
    coverage_note: str = "",
) -> str:
    """Render the markdown outcome summary appended to the task body (PRD §6.9).

    The human-facing triage: final disposition, the PR link, the verifier's
    coverage note, and any accumulated reflections (why a revise/escalate
    happened) so a supervisor can act from Notion without reading logs.
    """
    lines: list[str] = ["## Stromboli build summary", "", f"- **Outcome:** {status}"]
    if pr_url:
        lines.append(f"- **Pull request:** {pr_url}")
    else:
        lines.append("- **Pull request:** none opened.")
    if coverage_note:
        lines.append(f"- **Coverage:** {coverage_note}")
    if reflections:
        lines.append("")
        lines.append("### Reflections")
        lines.extend(f"- {entry}" for entry in reflections)
    return "\n".join(lines)


def resilient_append(
    notion: AppendGateway,
    page_id: str,
    markdown: str,
    *,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Append ``markdown`` to the task body with bounded retries.

    Returns ``True`` on success. On a permanent failure it logs and returns
    ``False`` — it never raises, so a write-back failure cannot crash a run.
    Backs off exponentially between attempts (``sleep`` is injectable for tests).
    """
    for attempt in range(1, retries + 1):
        try:
            notion.append_task_body(page_id, markdown)
            return True
        except Exception as exc:  # noqa: BLE001 - write-back must never propagate
            logger.warning(
                "Write-back to %s failed (attempt %d/%d): %s",
                page_id,
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
    logger.error("Write-back to %s gave up after %d attempts.", page_id, retries)
    return False


__all__ = [
    "ASSIGNEE_AGENT",
    "STATUS_COMPLETE",
    "STATUS_REVIEW",
    "STATUS_TODO",
    "STATUS_WORKING",
    "VALID_STATUSES",
    "AppendGateway",
    "NotionTaskClient",
    "Repo",
    "Task",
    "build_feedback_summary",
    "is_dispatchable",
    "parse_repo",
    "parse_task",
    "resilient_append",
]
