"""The FastAPI dispatch surface for the Stromboli worker.

A Notion automation (the *Ready checked* trigger) posts a task page id to
``POST /stromboli/dispatch`` through a Cloudflare Tunnel. The endpoint
authenticates the request with a shared secret and **enqueues** the build,
returning ``202 Accepted`` with its place in line — so a dispatch is never
dropped (the old fire-and-forget background task was rejected outright when a
build was already running) and the caller is never blocked on the build.

``GET /stromboli/status`` (same shared secret) returns the live queue snapshot —
what's running, what's waiting, and what recently finished — so you can see what
kicked off without reading logs.

Both the enqueue entrypoint and the status provider are injected, so this layer
is independently testable; production wiring passes the real queue consumer and
run ledger.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, Final

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

#: HTTP header carrying the shared dispatch secret. Documented in the README
#: alongside the Cloudflare Tunnel and Notion automation contract.
SECRET_HEADER: Final = "X-Stromboli-Secret"

#: Injected enqueue entrypoint: given a task page id, record it on the queue and
#: return its position in line (0 = next up).
EnqueueFn = Callable[[str], int]

#: Injected status provider: returns the current queue snapshot as plain data.
StatusProvider = Callable[[], dict[str, Any]]


class DispatchRequest(BaseModel):
    """Body of a dispatch request: the Notion task page id to build."""

    page_id: str = Field(min_length=1)


class DispatchResponse(BaseModel):
    """Acknowledgement returned immediately on a valid dispatch."""

    status: str
    page_id: str
    #: How many builds are ahead of this one in the queue (0 = next up).
    position: int


def _noop_enqueue(page_id: str) -> int:  # pragma: no cover - placeholder
    """Default enqueue entrypoint used until the real consumer is wired in."""
    return 0


def _empty_status() -> dict[str, Any]:  # pragma: no cover - placeholder
    return {"running": None, "queued": [], "recent": []}


def create_app(
    *,
    dispatch_secret: str,
    enqueue: EnqueueFn | None = None,
    status_provider: StatusProvider | None = None,
    on_startup: list[Callable[[], None]] | None = None,
    on_shutdown: list[Callable[[], None]] | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    :param dispatch_secret: the expected value of :data:`SECRET_HEADER`.
    :param enqueue: callable invoked with the task page id for each authenticated
        dispatch; returns the new run's queue position. Defaults to a no-op.
    :param status_provider: returns the live queue snapshot for ``GET
        /stromboli/status``. Defaults to an empty snapshot.
    :param on_startup / on_shutdown: lifecycle hooks (used to start/stop the
        background queue consumer).
    """

    enqueue_fn: EnqueueFn = enqueue or _noop_enqueue
    status_fn: StatusProvider = status_provider or _empty_status

    app = FastAPI(
        title="Stromboli",
        version="0.1.0",
        on_startup=on_startup or [],
        on_shutdown=on_shutdown or [],
    )

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
    def dispatch(request: DispatchRequest) -> DispatchResponse:
        # Enqueue synchronously (a fast ledger write) so the 202 truthfully means
        # "queued" — the consumer builds it serially in the background.
        position = enqueue_fn(request.page_id)
        return DispatchResponse(
            status="queued", page_id=request.page_id, position=position
        )

    @app.get(
        "/stromboli/status",
        dependencies=[Depends(require_secret)],
    )
    def status_endpoint() -> dict[str, Any]:
        return status_fn()

    return app


__all__ = [
    "SECRET_HEADER",
    "DispatchRequest",
    "DispatchResponse",
    "EnqueueFn",
    "StatusProvider",
    "create_app",
]
