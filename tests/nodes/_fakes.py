"""Shared test fakes for the graph nodes."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from stromboli.integrations.notion import Task

T = TypeVar("T", bound=BaseModel)


class FakeGateway:
    """A gateway whose ``structured`` returns a fixed payload, or raises."""

    def __init__(self, payload: dict[str, object] | None = None,
                 *, error: Exception | None = None) -> None:
        self._payload = payload or {}
        self._error = error
        self.calls: list[dict[str, str]] = []

    def structured(self, *, model: str, system: str, user: str, schema: type[T]) -> T:
        self.calls.append({"model": model, "system": system, "user": user})
        if self._error is not None:
            raise self._error
        return schema.model_validate(self._payload)


def make_task(page_id: str = "page-1", *, spec: str = "build X",
              name: str = "Task") -> Task:
    return Task(
        page_id=page_id, name=name, project_ids=("proj-1",), status="To do",
        spec=spec, assigned_to="Agent", ready=True, needs_review=False,
        pr_url=None, cost=None, tokens=None,
    )


class RoutingGateway:
    """A gateway that returns a payload chosen by the requested schema name.

    Lets one gateway serve both the Spec node (``schema=Spec``) and the verifier
    (``schema=Verdict``) in a single graph run.
    """

    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, str]] = []

    def structured(self, *, model: str, system: str, user: str, schema: type[T]) -> T:
        self.calls.append((schema.__name__, model))
        return schema.model_validate(self._payloads[schema.__name__])


class FakeNotion:
    """A Notion surface fake: serves one task and records appended notes."""

    def __init__(self, task: Task | None = None) -> None:
        self._task = task or make_task()
        self.appended: list[tuple[str, str]] = []

    def get_task(self, page_id: str) -> Task:
        return self._task

    def append_task_body(self, page_id: str, markdown: str) -> None:
        self.appended.append((page_id, markdown))
