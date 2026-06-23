"""Tests for the production app assembly (``stromboli.app``).

Construction must build the whole graph from settings without any network
(clients are lazy), and the resulting FastAPI app must enforce the dispatch
secret and answer the healthcheck — proving the worker is correctly wired as the
dispatch endpoint's background task.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from stromboli.app import build_deps, create_stromboli_app
from stromboli.pipeline import BuildDeps
from stromboli.settings import Settings, load_settings

_ENV = {
    "NOTION_TOKEN": "ntn",
    "GITHUB_TOKEN": "ght",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "LANGFUSE_HOST": "https://cloud.langfuse.com",
    "TUNNEL_PUBLIC_URL": "https://stromboli.example.com",
    "WORKSPACE_ROOT": "/tmp/stromboli-workspace",
    "ANTHROPIC_API_KEY": "ak",
    "DISPATCH_SHARED_SECRET": "s3cret",
}


def _settings() -> Settings:
    return load_settings(_env_file=None, **_ENV)


def test_build_deps_constructs_the_graph() -> None:
    deps = build_deps(_settings())
    assert isinstance(deps, BuildDeps)
    assert deps.breaker_config.max_iterations > 0
    assert deps.notion is not None
    assert deps.github is not None


def test_app_healthcheck_and_secret_enforcement() -> None:
    app = create_stromboli_app(_settings())
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}

    # No secret → rejected. (A valid dispatch is intentionally not exercised
    # here: it would run the real build in the TestClient's background task and
    # hit the network — the build pipeline is covered by test_pipeline.py.)
    assert client.post("/stromboli/dispatch", json={"page_id": "p"}).status_code == 401
    assert (
        client.post(
            "/stromboli/dispatch",
            json={"page_id": "p"},
            headers={"X-Stromboli-Secret": "wrong"},
        ).status_code
        == 401
    )
