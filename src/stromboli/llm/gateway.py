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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar

from pydantic import BaseModel

from stromboli.observability.tracing import current_span

logger = logging.getLogger(__name__)

#: Seconds between the two attempts at a gateway completion (transient 5xx /
#: connection blips must not escalate a whole task).
RETRY_DELAY_SECONDS: Final = 2.0

T = TypeVar("T", bound=BaseModel)

#: A ``litellm.completion``-shaped callable (kwargs-driven; returns a response).
CompletionFn = Callable[..., Any]


class Gateway(Protocol):
    """The reasoning-surface seam the spec/verifier/memory nodes depend on."""

    def structured(
        self, *, model: str, system: str, user: str, schema: type[T]
    ) -> T: ...

    def complete(self, *, model: str, system: str, user: str) -> str: ...


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


#: Cost weight for cache-read tokens (they bill at ~10% of input tokens). At
#: full weight a single real-repo coding pass "spends" millions and trips the
#: ceiling before any revise cycle is possible; the ceiling is a *cost*
#: backstop, so count them at cost.
CACHE_READ_WEIGHT: Final = 0.1


def usage_tokens(usage: dict[str, Any] | None) -> int:
    """Cost-weighted tokens in a usage record (LiteLLM- or SDK-shaped), or 0.

    Prefers an explicit ``total_tokens``; otherwise sums the input/output and
    cache-creation counters at full weight and cache reads at
    :data:`CACHE_READ_WEIGHT`. Accumulated onto ``state.tokens_used`` against
    the ``MAX_TOKENS_PER_TASK`` backstop (PRD §5).
    """
    if not usage:
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    counted = 0
    for key in (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cache_creation_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            counted += value
    cache_read = usage.get("cache_read_input_tokens")
    if isinstance(cache_read, int):
        counted += int(cache_read * CACHE_READ_WEIGHT)
    return counted


def _usage_dict(response: Any) -> dict[str, Any] | None:
    """Extract the usage record from a litellm/OpenAI-shaped response, if any."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None or isinstance(usage, dict):
        return usage
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001 - usage capture is best-effort
            return None
    return None


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
    #: Cap on output tokens — must be high enough that a structured reply isn't
    #: truncated mid-JSON (a truncated reply fails to parse). Reasoning models
    #: (e.g. Gemini flash/pro) spend *thinking* tokens from this same budget
    #: before emitting any JSON, so the cap must leave room for both.
    max_tokens: int = 16384
    #: The usage record of the most recent call (reset per call) — nodes read
    #: this to accumulate ``state.tokens_used`` (PRD §5 backstop ceiling).
    last_usage: dict[str, Any] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.completion is None:
            import litellm

            self.completion = litellm.completion

    def _record_usage(self, model: str, response: Any) -> None:
        """Capture the call's token usage: onto ``last_usage`` + a child span."""
        self.last_usage = _usage_dict(response)
        current_span().child(
            "llm-call",
            metadata={"model": model, "usage": self.last_usage},
        ).end()

    def _call(self, **kwargs: Any) -> Any:
        """One completion with a single retry on transient failure."""
        assert self.completion is not None  # set in __post_init__
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                return self.completion(**kwargs)
            except Exception as exc:  # noqa: BLE001 - typed at the boundary
                last = exc
                if attempt == 1:
                    logger.warning(
                        "Gateway call failed (attempt 1/2), retrying: %s", exc
                    )
                    time.sleep(RETRY_DELAY_SECONDS)
        raise GatewayError(f"Gateway completion failed: {last}") from last

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
        self.last_usage = None
        messages = [
            {"role": "system", "content": f"{system}\n\n{_schema_instructions(schema)}"},
            {"role": "user", "content": user},
        ]
        response = self._call(
            model=self._route(model),
            messages=messages,
            api_base=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        self._record_usage(model, response)
        content = _extract_content(response)
        return schema.model_validate(_coerce_json(content))

    def complete(self, *, model: str, system: str, user: str) -> str:
        """Run one plain-text completion (no JSON coercion) and return the text."""
        self.last_usage = None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response = self._call(
            model=self._route(model),
            messages=messages,
            api_base=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        self._record_usage(model, response)
        return _extract_content(response).strip()


def build_gateway(*, base_url: str, api_key: str) -> LiteLLMGateway:
    """Construct the production gateway from the proxy base URL + key."""
    return LiteLLMGateway(base_url=base_url.rstrip("/"), api_key=api_key)


__all__ = [
    "CompletionFn",
    "Gateway",
    "GatewayError",
    "LiteLLMGateway",
    "build_gateway",
    "usage_tokens",
]
