"""Tests for the LiteLLM structured-output gateway."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from stromboli.llm.gateway import GatewayError, LiteLLMGateway, build_gateway
from stromboli.state import Spec


def _response(content: str) -> SimpleNamespace:
    """A litellm/OpenAI-shaped response object."""
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_structured_parses_into_schema() -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _response(
            json.dumps(
                {
                    "goal": "add a flag",
                    "acceptance_criteria": ["flag toggles X"],
                    "affected_paths": ["src/a.py"],
                    "constraints": [],
                    "ambiguous": False,
                }
            )
        )

    gw = LiteLLMGateway(base_url="http://proxy", api_key="k", completion=fake_completion)
    spec = gw.structured(model="m", system="sys", user="do x", schema=Spec)
    assert isinstance(spec, Spec)
    assert spec.goal == "add a flag"
    # The proxy base + key + JSON response format are passed through.
    assert captured["api_base"] == "http://proxy"
    assert captured["api_key"] == "k"
    assert captured["response_format"] == {"type": "json_object"}
    # The schema instructions are appended to the system prompt.
    assert "JSON schema" in captured["messages"][0]["content"]


def test_structured_raises_on_bad_json() -> None:
    gw = LiteLLMGateway(
        base_url="http://p", api_key="k", completion=lambda **_k: _response("not json")
    )
    with pytest.raises(GatewayError):
        gw.structured(model="m", system="s", user="u", schema=Spec)


def test_structured_raises_on_completion_failure() -> None:
    def boom(**_k: Any) -> Any:
        raise RuntimeError("proxy down")

    gw = LiteLLMGateway(base_url="http://p", api_key="k", completion=boom)
    with pytest.raises(GatewayError):
        gw.structured(model="m", system="s", user="u", schema=Spec)


def test_build_gateway_strips_trailing_slash() -> None:
    gw = build_gateway(base_url="http://proxy/", api_key="k")
    assert gw.base_url == "http://proxy"
