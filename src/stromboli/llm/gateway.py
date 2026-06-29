"""The LiteLLM gateway — single structured calls for the reasoning surface.

PRD §4: the spec / router / memory nodes and the verifier are *single structured
calls* (no file/bash tools), so a gateway is the right fit — and the gateway is
what lets the verifier run on an independent, non-Claude model. This wraps
``litellm.completion`` behind one method, :meth:`Gateway.structured`, that coerces
the model's reply into a Pydantic schema (the same nested models the graph state
uses), so callers never parse raw text.

The completion callable is injected (default: ``litellm.completion``) so node
unit tests run with a fake and never make a network call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

#: A ``litellm.completion``-shaped callable (kwargs-driven; returns a response).
CompletionFn = Callable[..., Any]


class Gateway(Protocol):
    """The reasoning-surface seam the spec/verifier/memory nodes depend on."""

    def structured(
        self, *, model: str, system: str, user: str, schema: type[T]
    ) -> T: ...


def _extract_content(response: Any) -> str:
    """Pull the assistant message text out of a litellm/OpenAI-shaped response."""
    # litellm returns a ModelResponse with ``.choices[0].message.content``; also
    # tolerate a plain dict (some providers / fakes).
    try:
        choices = (
            response.choices if hasattr(response, "choices") else response["choices"]
        )
        first = choices[0]
        message = first.message if hasattr(first, "message") else first["message"]
        content = (
            message.content if hasattr(message, "content") else message["content"]
        )
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        raise GatewayError(f"Malformed gateway response: {exc}") from exc
    if not isinstance(content, str):
        raise GatewayError("Gateway response content was not text")
    return content


class GatewayError(RuntimeError):
    """The gateway call failed or returned an unusable / unparseable reply."""


def _coerce_json(content: str) -> dict[str, Any]:
    """Parse a JSON object from a model reply, tolerating fences / prose.

    Models vary: some honor ``response_format`` and return bare JSON, others wrap
    it in ```json fences or add a sentence. Try a direct parse, then a fenced
    block, then the first balanced ``{…}`` span.
    """
    text = content.strip()
    candidates: list[str] = []
    if text:
        candidates.append(text)
    if "```" in text:
        # Strip a leading ```json / ``` fence and the trailing fence.
        inner = text.split("```", 2)
        if len(inner) >= 2:
            block = inner[1]
            if block.lower().startswith("json"):
                block = block[4:]
            candidates.append(block.strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise GatewayError(f"Gateway reply was not valid JSON: {content[:200]!r}")


def _schema_instructions(schema: type[BaseModel]) -> str:
    """A system-prompt suffix telling the model to emit JSON for ``schema``."""
    return (
        "Respond with a single JSON object — no prose, no code fences — matching "
        "this JSON schema exactly:\n"
        f"{json.dumps(schema.model_json_schema())}"
    )


@dataclass
class LiteLLMGateway:
    """A structured-output gateway backed by ``litellm.completion``.

    ``base_url`` + ``api_key`` target the LiteLLM proxy (PRD §4), which maps the
    model name to a provider — so the non-Claude verifier needs no separate key.
    """

    base_url: str
    api_key: str
    completion: CompletionFn | None = None
    temperature: float = 0.0

    def __post_init__(self) -> None:
        if self.completion is None:
            import litellm

            self.completion = litellm.completion

    @staticmethod
    def _route(model: str) -> str:
        """Force the call through the proxy's OpenAI-compatible path.

        Without the ``litellm_proxy/`` prefix, litellm infers the provider from
        the model name (e.g. ``claude-*`` → Anthropic's ``/v1/messages``), which
        bypasses the proxy's routing/model-group mapping. The prefix makes it use
        the proxy's ``/chat/completions`` so the model name is resolved by the
        proxy, not by litellm's provider heuristic.
        """
        if model.startswith(("litellm_proxy/", "openai/")):
            return model
        return f"litellm_proxy/{model}"

    def structured(
        self, *, model: str, system: str, user: str, schema: type[T]
    ) -> T:
        """Run one structured call and coerce the reply into ``schema``."""
        assert self.completion is not None  # set in __post_init__
        messages = [
            {"role": "system", "content": f"{system}\n\n{_schema_instructions(schema)}"},
            {"role": "user", "content": user},
        ]
        try:
            response = self.completion(
                model=self._route(model),
                messages=messages,
                api_base=self.base_url,
                api_key=self.api_key,
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - surface as a typed gateway error
            raise GatewayError(f"Gateway completion failed: {exc}") from exc

        content = _extract_content(response)
        return schema.model_validate(_coerce_json(content))


def build_gateway(*, base_url: str, api_key: str) -> LiteLLMGateway:
    """Construct the production gateway from the proxy base URL + key."""
    return LiteLLMGateway(base_url=base_url.rstrip("/"), api_key=api_key)


__all__ = [
    "CompletionFn",
    "Gateway",
    "GatewayError",
    "LiteLLMGateway",
    "build_gateway",
]
