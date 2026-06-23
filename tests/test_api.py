"""Tests for :mod:`stromboli.api` — the FastAPI dispatch surface.

These assert the externally-observable contract of US-003:

* ``GET /healthz`` returns 200.
* ``POST /stromboli/dispatch`` requires the shared-secret header — a missing or
  wrong secret yields 401 and the build is **not** scheduled.
* A valid request returns 202 immediately and schedules the build with the
  page id from the body (the actual build is processed asynchronously).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from stromboli.api import SECRET_HEADER, create_app

SECRET = "shared-secret"
PAGE_ID = "page-123"


def _client(scheduled: list[str]) -> TestClient:
    """Build a test client whose dispatcher records scheduled page ids."""

    def process_task(page_id: str) -> None:
        scheduled.append(page_id)

    app = create_app(dispatch_secret=SECRET, process_task=process_task)
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


def test_dispatch_valid_request_returns_202_and_schedules_build() -> None:
    scheduled: list[str] = []
    client = _client(scheduled)

    response = client.post(
        "/stromboli/dispatch",
        json={"page_id": PAGE_ID},
        headers={SECRET_HEADER: SECRET},
    )

    assert response.status_code == 202
    # TestClient runs background tasks after the response is sent.
    assert scheduled == [PAGE_ID]


def test_dispatch_returns_the_accepted_page_id() -> None:
    client = _client([])

    response = client.post(
        "/stromboli/dispatch",
        json={"page_id": PAGE_ID},
        headers={SECRET_HEADER: SECRET},
    )

    assert response.status_code == 202
    assert response.json()["page_id"] == PAGE_ID


def test_dispatch_rejects_body_without_page_id() -> None:
    client = _client([])

    response = client.post(
        "/stromboli/dispatch",
        json={},
        headers={SECRET_HEADER: SECRET},
    )

    # Authenticated but malformed body -> 422 validation error, not scheduled.
    assert response.status_code == 422
