"""Project context loader — feed the project's context file into the Spec node.

A Notion **Project** carries a ``Context Root`` property: a GitHub *blob* URL to
the project's context file (e.g. ``README.md`` / ``CLAUDE.md``). Before the Spec
model runs, we fetch that file and inject it as project conventions, so a task is
specced with the project's context assumed (design: docs/... investigate-loop
sibling; brainstormed 2026-06-30).

Self-contained URL → no filename convention, no repo cross-referencing. Anything
missing (no Project, blank ``Context Root``, 404, parse failure) degrades to an
empty string, so the Spec node simply proceeds without context.
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Callable
from typing import Any

from stromboli.state import StromboliState

logger = logging.getLogger(__name__)

#: ``Callable[[blob_url], file_text | None]`` — fetch a GitHub file's content.
FileFetcher = Callable[[str], "str | None"]

#: How much of the context file to inject (head-biased; conventions lead).
DEFAULT_CONTEXT_BUDGET = 8000

_BLOB_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/blob/"
    r"(?P<ref>[^/]+)/(?P<path>.+)"
)


def parse_blob_url(url: str) -> tuple[str, str, str, str] | None:
    """``…/blob/<ref>/<path>`` → ``(owner, repo, ref, path)``, or None."""
    match = _BLOB_RE.fullmatch(url.strip())
    if match is None:
        return None
    return (
        match.group("owner"), match.group("repo"),
        match.group("ref"), match.group("path"),
    )


def github_file_fetcher(token: str, *, timeout: float = 30.0) -> FileFetcher:
    """Build a :data:`FileFetcher` that reads a blob URL via the GitHub API."""
    import httpx

    def fetch(url: str) -> str | None:
        parsed = parse_blob_url(url)
        if parsed is None:
            logger.warning("Context Root is not a GitHub blob URL: %s", url)
            return None
        owner, repo, ref, path = parsed
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        try:
            resp = httpx.get(
                api,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                logger.warning("Context Root fetch %s → %s", url, resp.status_code)
                return None
            return base64.b64decode(resp.json()["content"]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - context is best-effort, never fatal
            logger.warning("Context Root fetch failed for %s", url, exc_info=True)
            return None

    return fetch


def make_project_context(
    notion: Any,
    *,
    fetch: FileFetcher,
    budget: int = DEFAULT_CONTEXT_BUDGET,
) -> Callable[[StromboliState], str]:
    """Build the Spec-node ``project_context`` seam.

    Resolves the task's Project ``Context Root`` URL, fetches the file, and
    returns a labelled, length-capped block (or ``""`` when anything is missing).
    Caches per URL for the process lifetime (the watcher is long-running).
    """
    cache: dict[str, str] = {}

    def load(state: StromboliState) -> str:
        try:
            task = notion.get_task(state.task_id)
            url = notion.get_project_context_url(task)
        except Exception:  # noqa: BLE001 - never block specccing on context lookup
            logger.warning("Could not resolve Context Root for %s", state.task_id)
            return ""
        if not url:
            return ""
        if url in cache:
            return cache[url]
        content = fetch(url)
        block = (
            f"Project conventions (from {url}):\n{content[:budget]}" if content else ""
        )
        cache[url] = block
        return block

    return load


__all__ = [
    "DEFAULT_CONTEXT_BUDGET",
    "FileFetcher",
    "github_file_fetcher",
    "make_project_context",
    "parse_blob_url",
]
