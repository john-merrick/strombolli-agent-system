"""Tests for :mod:`stromboli.api` — the FastAPI dispatch surface.

These assert the externally-observable contract:

* ``GET /healthz`` returns 200.
* ``POST /stromboli/dispatch`` requires the shared-secret header — a missing or
  wrong secret yields 401 and nothing is enqueued.
* A valid request returns 202 and **enqueues** the page id, reporting its
  position in line.
* ``GET /stromboli/status`` requires the secret and returns the queue snapshot.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from stromboli.api import SECRET_HEADER, create_app

SECRET = "shared-secret"
PAGE_ID = "page-123"


def _client(
    scheduled: list[str],
    *,
    position: int = 0,
    status: dict[str, Any] | None = None,
) -> TestClient:
    """Build a test client whose enqueue records page ids and returns a position."""

    def enqueue(page_id: str) -> int:
        scheduled.append(page_id)
        return position

    app = create_app(
        dispatch_secret=SECRET,
        enqueue=enqueue,
        status_provider=lambda: status or {"running": None, "queued": [], "recent": []},
    )
    return TestClient(app)


def test_healthz_returns_200() -> None:
    client = _client([])

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dispatch_missing_secret_returns_401() -> None:
    scheduled: list[str] = []
    client = _client(scheduled)

    response = client.post("/stromboli/dispatch", json={"page_id": PAGE_ID})

    assert response.status_code == 401
    # The build must not have been scheduled for an unauthenticated request.
    assert scheduled == []


def test_dispatch_invalid_secret_returns_401() -> None:
    scheduled: list[str] = []
    client = _client(scheduled)

    response = client.post(
        "/stromboli/dispatch",
        json={"page_id": PAGE_ID},
        headers={SECRET_HEADER: "wrong-secret"},
    )

    assert response.status_code == 401
    assert scheduled == []


def test_dispatch_valid_request_returns_202_and_enqueues() -> None:
    scheduled: list[str] = []
    client = _client(scheduled, position=2)

    response = client.post(
        "/stromboli/dispatch",
        json={"page_id": PAGE_ID},
        headers={SECRET_HEADER: SECRET},
    )

    assert response.status_code == 202
    assert scheduled == [PAGE_ID]
    body = response.json()
    assert body["status"] == "queued"
    assert body["position"] == 2


def test_dispatch_returns_the_accepted_page_id() -> None:
    client = _client([])

    response = client.post(
        "/stromboli/dispatch",
        json={"page_id": PAGE_ID},
        headers={SECRET_HEADER: SECRET},
    )

    assert response.status_code == 202
    assert response.json()["page_id"] == PAGE_ID


def test_status_requires_the_secret() -> None:
    client = _client([])
    assert client.get("/stromboli/status").status_code == 401


def test_status_returns_the_queue_snapshot() -> None:
    snapshot = {
        "running": {"id": 1, "page_id": "p1", "state": "running"},
        "queued": [{"id": 2, "page_id": "p2", "state": "queued"}],
        "recent": [],
    }
    client = _client([], status=snapshot)

    response = client.get("/stromboli/status", headers={SECRET_HEADER: SECRET})

    assert response.status_code == 200
    assert response.json() == snapshot


def test_dispatch_rejects_body_without_page_id() -> None:
    client = _client([])

    response = client.post(
        "/stromboli/dispatch",
        json={},
        headers={SECRET_HEADER: SECRET},
    )

    # Authenticated but malformed body -> 422 validation error, not scheduled.
    assert response.status_code == 422
