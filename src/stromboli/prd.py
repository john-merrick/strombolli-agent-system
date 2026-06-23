"""Compile a Notion task Spec into a Ralph ``prd.json`` (US-006).

The worker drives a target repository through the Ralph loop, which reads a
``{meta, items}`` PRD from ``scripts/prd.json`` inside the worktree. This module
turns a task's free-text **Spec** — a markdown string listing the acceptance
criteria — into that envelope:

* each acceptance criterion becomes one item with
  ``passes:false / attempts:0 / blocked:false`` (and an empty ``blockReason``),
  so the loop sees N fresh, eligible items for N criteria;
* ``meta.maxAttempts`` carries the per-item attempt ceiling K; and
* ``meta.branchName`` is set to the **exact** US-005 worktree branch so the loop
  commits onto the branch the worker prepared.

Criterion parsing is a pure function so the "N criteria → N items" contract is
unit-testable against raw spec strings, and :func:`write_prd` is the only part
that touches disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

from stromboli.notion import Task

#: Default per-item attempt ceiling (K) baked into a compiled PRD's ``meta``.
DEFAULT_MAX_ATTEMPTS: Final = 3
#: Where the Ralph loop reads its PRD, relative to the worktree root.
PRD_RELATIVE_PATH: Final = Path("scripts/prd.json")
#: Block id / name assigned to every generated acceptance-criterion item.
ITEM_BLOCK: Final = "AC"
ITEM_BLOCK_NAME: Final = "Acceptance Criteria"

#: A markdown list item: ``-``/``*``/``+`` or ``1.``/``1)`` markers.
_LIST_RE: Final = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.*\S)\s*$")
#: A markdown ATX heading (``#`` .. ``######``).
_HEADING_RE: Final = re.compile(r"^\s*#{1,6}\s+(?P<text>.*\S)\s*$")


def _scope_to_acceptance(lines: list[str]) -> list[str]:
    """Narrow ``lines`` to an "Acceptance Criteria" section if one is present.

    When a heading whose text mentions *acceptance* exists, only the lines from
    just after it up to the next heading are returned; otherwise the whole
    document is considered (a bare list of criteria is the common case).
    """
    start: int | None = None
    for i, line in enumerate(lines):
        heading = _HEADING_RE.match(line)
        if heading and "acceptance" in heading.group("text").lower():
            start = i + 1
            break
    if start is None:
        return lines

    section: list[str] = []
    for line in lines[start:]:
        if _HEADING_RE.match(line):
            break
        section.append(line)
    return section


def parse_acceptance_criteria(spec: str) -> list[str]:
    """Extract the ordered acceptance criteria from a task ``spec``.

    Markdown list items (``-``/``*``/``+``/``1.``) are preferred; their order is
    preserved. If the spec has no list markers, each non-blank, non-heading line
    is treated as a criterion. Returns ``[]`` for an empty/whitespace spec.
    """
    lines = _scope_to_acceptance(spec.splitlines())

    criteria = [
        match.group("text").strip()
        for line in lines
        if (match := _LIST_RE.match(line))
    ]
    if criteria:
        return criteria

    return [
        line.strip()
        for line in lines
        if line.strip() and not _HEADING_RE.match(line)
    ]


def build_prd(
    task: Task,
    *,
    branch: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Compile ``task`` into a Ralph ``{meta, items}`` PRD for ``branch``.

    Each acceptance criterion in the task Spec becomes one fresh item. The
    returned ``meta`` carries ``maxAttempts`` (K) and ``branchName`` set to the
    US-005 worktree branch. Raises :class:`ValueError` when the Spec yields no
    acceptance criteria — a task with nothing to build must not be dispatched.
    """
    criteria = parse_acceptance_criteria(task.spec)
    if not criteria:
        raise ValueError(
            f"Task {task.page_id} Spec has no acceptance criteria to compile"
        )

    items = [
        {
            "id": f"{ITEM_BLOCK}-{i:03d}",
            "block": ITEM_BLOCK,
            "blockName": ITEM_BLOCK_NAME,
            "description": criterion,
            "passes": False,
            "attempts": 0,
            "blocked": False,
            "blockReason": "",
        }
        for i, criterion in enumerate(criteria, start=1)
    ]

    return {
        "meta": {
            "project": task.name,
            "branchName": branch,
            "maxAttempts": max_attempts,
        },
        "items": items,
    }


def write_prd(
    worktree_root: str | Path,
    prd: dict[str, Any],
    *,
    relative_path: Path = PRD_RELATIVE_PATH,
) -> Path:
    """Write ``prd`` as JSON into the worktree where Ralph will read it.

    Creates the parent directory if needed and returns the path written.
    """
    target = Path(worktree_root) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prd, indent=2) + "\n", encoding="utf-8")
    return target


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "ITEM_BLOCK",
    "ITEM_BLOCK_NAME",
    "PRD_RELATIVE_PATH",
    "build_prd",
    "parse_acceptance_criteria",
    "write_prd",
]
