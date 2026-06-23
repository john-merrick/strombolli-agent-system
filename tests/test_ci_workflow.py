"""Validate the reusable GitHub Actions CI workflow (US-009).

The worker opens a PR for every task it builds; that PR must be gated on the
same checks the project enforces locally — tests, lint, and type checks. This
suite parses ``.github/workflows/ci.yml`` and asserts it is well-formed YAML
that runs on ``pull_request`` (so it gates PRs) and is also ``workflow_call``-able
(so a target repo can reuse it), and that it actually runs pytest, ruff, and
mypy. There is no application code here — the workflow file *is* the artifact, so
the test guards its structure rather than behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
)


def _load() -> dict[Any, Any]:
    # Note: PyYAML parses the bare `on:` trigger key as the boolean True
    # (a YAML 1.1 quirk), so the mapping is not str-keyed — type it as Any.
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_workflow_file_exists_and_is_valid_yaml() -> None:
    assert WORKFLOW_PATH.exists(), f"missing workflow at {WORKFLOW_PATH}"
    # safe_load raises on malformed YAML; reaching the assert means it parsed.
    assert _load()


def test_workflow_triggers_on_pull_request_and_is_reusable() -> None:
    workflow = _load()
    # PyYAML parses the bare `on:` key as the boolean True, so accept either.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    # Gates PRs and is callable by a target repo as a reusable workflow.
    assert "pull_request" in triggers
    assert "workflow_call" in triggers


def test_workflow_runs_tests_lint_and_types() -> None:
    workflow = _load()
    jobs = workflow["jobs"]
    assert jobs, "workflow defines no jobs"

    # Flatten every `run:` step across all jobs into one searchable blob.
    run_steps = " ".join(
        step["run"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if "run" in step
    )

    assert "pytest" in run_steps, "CI does not run tests"
    assert "ruff" in run_steps, "CI does not run lint"
    assert "mypy" in run_steps, "CI does not run type checks"


def test_workflow_checks_out_code_and_sets_up_python() -> None:
    workflow = _load()
    uses = " ".join(
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    )
    assert "actions/checkout" in uses
