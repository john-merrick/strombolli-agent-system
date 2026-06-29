"""Tests for the Agent SDK coder wrapper (PRD §6.4) — with a fake SDK query."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from stromboli.llm.coder import AgentCoder, CoderError, RateLimitError


def _assistant(*tools: str, text: str = "") -> AssistantMessage:
    content: list[Any] = [
        ToolUseBlock(id=f"t{i}", name=name, input={}) for i, name in enumerate(tools)
    ]
    if text:
        content.append(TextBlock(text=text))
    return AssistantMessage(content=content, model="claude", usage={"output_tokens": 10})


def _result(subtype: str = "success", *, is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=2,
        session_id="sess-1",
        total_cost_usd=0.01,
    )


def _fake_query(messages: list[Any]) -> tuple[Any, dict[str, Any]]:
    captured: dict[str, Any] = {}

    async def query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        captured["prompt"] = prompt
        captured["options"] = options
        for m in messages:
            yield m

    return query, captured


def test_coder_captures_diff_session_and_turns() -> None:
    query, captured = _fake_query(
        [_assistant("Read", "Edit", text="done"), _result("success")]
    )
    coder = AgentCoder(
        model="claude-opus-4-8",
        api_key="sk-platform",
        auth_mode="api_key",
        query_fn=query,
        diff_fn=lambda _p: "diff --git a/x b/x\n+ok",
        max_turns=10,
    )
    run = coder.run("build the thing", "/tmp/wt")
    assert run.clean is True
    assert run.session_id == "sess-1"
    assert run.turns == 2
    assert run.diff.endswith("+ok")
    assert run.turn_records[0].tools == ("Read", "Edit")
    # The bounded options carried the allowlist + max_turns + platform key.
    opts = captured["options"]
    assert opts.max_turns == 10
    assert "Bash" in opts.allowed_tools
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-platform"
    assert opts.cwd == "/tmp/wt"


def test_max_turns_is_a_clean_budget_exit() -> None:
    query, _ = _fake_query([_assistant("Bash"), _result("error_max_turns")])
    coder = AgentCoder(model="m", api_key="k", query_fn=query, diff_fn=lambda _p: "")
    run = coder.run("impossible", "/tmp/wt")
    # A bounded budget exit is clean (no unbounded spin), not an execution error.
    assert run.clean is True
    assert run.subtype == "error_max_turns"


def test_execution_error_is_not_clean() -> None:
    query, _ = _fake_query([_result("error_during_execution", is_error=True)])
    coder = AgentCoder(model="m", api_key="k", query_fn=query, diff_fn=lambda _p: "")
    run = coder.run("x", "/tmp/wt")
    assert run.clean is False


def test_missing_result_message_raises() -> None:
    query, _ = _fake_query([_assistant("Read")])  # no ResultMessage
    coder = AgentCoder(model="m", api_key="k", query_fn=query, diff_fn=lambda _p: "")
    with pytest.raises(CoderError):
        coder.run("x", "/tmp/wt")


def test_resume_is_passed_through() -> None:
    query, captured = _fake_query([_result("success")])
    coder = AgentCoder(model="m", api_key="k", query_fn=query, diff_fn=lambda _p: "")
    coder.run("revise", "/tmp/wt", resume="sess-prev")
    assert captured["options"].resume == "sess-prev"


def test_permission_gate_allows_allowlist_denies_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    coder = AgentCoder(model="m", allowed_tools=("Read", "Bash"))

    async def check() -> None:
        allow = await coder._gate("Read", {}, None)  # type: ignore[arg-type]
        deny = await coder._gate("WebFetch", {}, None)  # type: ignore[arg-type]
        assert allow.behavior == "allow"
        assert deny.behavior == "deny"
        assert deny.interrupt is False  # fail closed, never hang

    asyncio.run(check())


# --- auth modes (PRD §4a) ------------------------------------------------- #
def test_subscription_mode_passes_no_api_key_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    query, captured = _fake_query([_result("success")])
    coder = AgentCoder(model="m", query_fn=query, diff_fn=lambda _p: "")
    assert coder.auth_mode == "subscription"  # the default
    coder.run("x", "/tmp/wt")
    # No api key is handed to the SDK → it runs on the logged-in plan tokens.
    assert "ANTHROPIC_API_KEY" not in captured["options"].env


def test_subscription_guardrail_rejects_stray_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stray")
    with pytest.raises(CoderError):
        AgentCoder(model="m")  # subscription default + stray key → fail closed


def test_api_key_mode_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CoderError):
        AgentCoder(model="m", auth_mode="api_key")  # no key supplied


def _raising_query(messages: list[Any], exc: Exception) -> Any:
    async def query(*, prompt: Any, options: Any) -> AsyncIterator[Any]:
        for m in messages:
            yield m
        raise exc

    return query


# --- SDK-raised terminals in streaming mode (PRD §6.4 / §4a) -------------- #
def test_sdk_raised_max_turns_is_clean_budget_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    q = _raising_query(
        [_assistant("Bash")],
        Exception("Claude Code returned an error result: Reached maximum number of turns (25)"),
    )
    coder = AgentCoder(model="m", query_fn=q, diff_fn=lambda _p: "diff --git a\n+x")
    run = coder.run("x", "/tmp/wt")
    # Bounded budget exit — captured, not a crash; the partial diff is kept.
    assert run.clean is True
    assert run.subtype == "error_max_turns"
    assert run.diff == "diff --git a\n+x"


def test_sdk_raised_rate_limit_becomes_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    q = _raising_query([_assistant("Bash")], Exception("API rate limit exceeded"))
    coder = AgentCoder(model="m", query_fn=q, diff_fn=lambda _p: "")
    with pytest.raises(RateLimitError):
        coder.run("x", "/tmp/wt")


def test_sdk_raised_other_error_is_coder_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    q = _raising_query([], Exception("unexpected boom"))
    coder = AgentCoder(model="m", query_fn=q, diff_fn=lambda _p: "")
    with pytest.raises(CoderError):
        coder.run("x", "/tmp/wt")


# --- rate-limit cutoff (PRD §4a) ------------------------------------------ #
def test_rate_limit_raises_retryable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    event = RateLimitEvent(
        rate_limit_info=SimpleNamespace(  # type: ignore[arg-type]
            status="rejected", resets_at="2026-07-01T00:00:00Z"
        ),
        uuid="rl-1",
        session_id="sess-rl",
    )
    query, _ = _fake_query([_assistant("Bash"), event])
    coder = AgentCoder(model="m", query_fn=query, diff_fn=lambda _p: "")
    with pytest.raises(RateLimitError) as exc:
        coder.run("x", "/tmp/wt")
    # The session is captured so the caller can resume after the window.
    assert exc.value.session_id == "sess-rl"
    assert exc.value.resets_at == "2026-07-01T00:00:00Z"
