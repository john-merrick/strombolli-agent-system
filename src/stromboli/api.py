"""The FastAPI dispatch surface for the Stromboli worker.

A Notion automation (the *Ready checked* trigger) posts a task page id to
``POST /stromboli/dispatch`` through a Cloudflare Tunnel. The endpoint
authenticates the request with a shared secret, returns ``202 Accepted``
immediately, and processes the build asynchronously via a FastAPI background
task so the caller (Notion) is never kept waiting.

The actual build pipeline (claim guard, worktree, Ralph loop, PR, write-back)
is wired in later stories. To keep this layer independently testable, the
build entrypoint is injected as ``process_task`` — tests pass a recording stub,
and production wiring will pass the real worker.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Final

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

#: HTTP header carrying the shared dispatch secret. Documented in the README
#: alongside the Cloudflare Tunnel and Notion automation contract.
SECRET_HEADER: Final = "X-Stromboli-Secret"

#: Type of the injected build entrypoint: given a task page id, run the build.
ProcessTask = Callable[[str], None]


class DispatchRequest(BaseModel):
    """Body of a dispatch request: the Notion task page id to build."""

    page_id: str = Field(min_length=1)


class DispatchResponse(BaseModel):
    """Acknowledgement returned immediately on a valid dispatch."""

    status: str
    page_id: str


def _noop_process_task(page_id: str) -> None:  # pragma: no cover - placeholder
    """Default build entrypoint used until the real worker is wired in."""


def create_app(
    *,
    dispatch_secret: str,
    process_task: ProcessTask | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    :param dispatch_secret: the expected value of :data:`SECRET_HEADER`.
    :param process_task: callable invoked (in the background) with the task page
        id for each authenticated dispatch. Defaults to a no-op placeholder.
    """

    handler: ProcessTask = process_task or _noop_process_task

    app = FastAPI(title="Stromboli", version="0.1.0")

    def require_secret(
        provided: str | None = Header(default=None, alias=SECRET_HEADER),
    ) -> None:
        # Reject a missing or mismatched secret. ``compare_digest`` keeps the
        # check constant-time so the comparison does not leak the secret.
        if provided is None or not secrets.compare_digest(provided, dispatch_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid dispatch secret.",
            )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/stromboli/dispatch",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=DispatchResponse,
        dependencies=[Depends(require_secret)],
    )
    def dispatch(
        request: DispatchRequest,
        background_tasks: BackgroundTasks,
    ) -> DispatchResponse:
        # Hand the build off to the background and acknowledge immediately so
        # the Notion automation is not blocked on the build duration.
        background_tasks.add_task(handler, request.page_id)
        return DispatchResponse(status="accepted", page_id=request.page_id)

    return app


__all__ = [
    "SECRET_HEADER",
    "DispatchRequest",
    "DispatchResponse",
    "create_app",
]
