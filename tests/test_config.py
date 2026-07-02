"""Tests for the static budgets/models config (PRD §4 / §5)."""

from __future__ import annotations

import pytest

from stromboli.config import (
    DEFAULT_CODER_MODEL,
    DEFAULT_VERIFIER_MODEL,
    Budgets,
    Models,
    from_settings,
)
from stromboli.settings import load_settings

_ENV = {
    "NOTION_TOKEN": "n",
    "NOTION_TASK_DB_ID": "db",
    "GITHUB_TOKEN": "g",
    "LITELLM_BASE_URL": "http://proxy",
    "LITELLM_API_KEY": "k",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "LANGFUSE_HOST": "http://lf",
    "WORKSPACE_ROOT": "/tmp/ws",
}


def test_budgets_defaults_are_sane() -> None:
    b = Budgets()
    assert b.max_inner_turns >= 1
    assert b.max_outer_revisions >= 0
    assert b.max_tokens_per_task >= 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_inner_turns": 0},
        {"max_outer_revisions": -1},
        {"max_tokens_per_task": 0},
    ],
)
def test_budgets_reject_invalid(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        Budgets(**kwargs)


def test_models_defaults_split_surfaces() -> None:
    m = Models()
    assert m.coder == DEFAULT_CODER_MODEL
    assert m.verifier == DEFAULT_VERIFIER_MODEL
    # The verifier must be a different family from the coder (PRD §11.1).
    assert m.verifier != m.coder


def test_from_settings_maps_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Budget vars may leak in from the host env (.env autoload by a dependency).
    for key in ("MAX_INNER_TURNS", "MAX_OUTER_REVISIONS", "MAX_TOKENS_PER_TASK"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(_env_file=None, **_ENV)
    config = from_settings(settings)
    assert config.budgets.max_inner_turns == 25
    assert config.models.coder == DEFAULT_CODER_MODEL
    assert "gemini" in config.models.verifier
