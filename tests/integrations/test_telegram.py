"""Tests for the best-effort Telegram notifier."""

from __future__ import annotations

from typing import Any

from stromboli.integrations.telegram import (
    NullNotifier,
    TelegramNotifier,
    make_notifier,
    parse_updates,
    telegram_fetcher,
    telegram_sender,
)


def test_make_notifier_null_when_unconfigured() -> None:
    assert isinstance(make_notifier(None, None), NullNotifier)
    assert isinstance(make_notifier("tok", None), NullNotifier)


def test_make_notifier_real_when_configured() -> None:
    posts: list[tuple[str, dict[str, str]]] = []
    notifier = make_notifier("tok", "chat", post=lambda u, p: posts.append((u, p)))
    assert isinstance(notifier, TelegramNotifier)
    notifier.done("task-1", "https://pr")
    assert posts and posts[0][1]["chat_id"] == "chat"
    assert "Done" in posts[0][1]["text"]


def test_escalation_message() -> None:
    posts: list[dict[str, str]] = []
    notifier = make_notifier("tok", "chat", post=lambda _u, p: posts.append(p))
    notifier.escalation("task-9", "stuck after 3 revisions")
    assert "Escalation" in posts[0]["text"]
    assert "task-9" in posts[0]["text"]


def test_send_failure_is_swallowed() -> None:
    def boom(_u: str, _p: dict[str, str]) -> None:
        raise RuntimeError("telegram down")

    notifier = TelegramNotifier(send=telegram_sender("t", "c", post=boom))
    # Must not raise — a notification never breaks a run.
    notifier.notify("hello")


def test_null_notifier_is_noop() -> None:
    NullNotifier().done("t", None)  # no exception, no output


def test_parse_updates_keeps_text_skips_other() -> None:
    payload = {
        "result": [
            {"update_id": 10, "message": {"chat": {"id": 42}, "text": "#1 hi"}},
            {"update_id": 11, "message": {"chat": {"id": 42}}},  # no text → skipped
            {"update_id": 12, "edited_message": {"chat": {"id": 7}, "text": "edit"}},
        ]
    }
    updates = parse_updates(payload)
    assert [(u.update_id, u.chat_id, u.text) for u in updates] == [
        (10, "42", "#1 hi"),
        (12, "7", "edit"),
    ]


def test_telegram_fetcher_passes_offset() -> None:
    seen: list[dict[str, str]] = []

    def fake_get(_url: str, params: dict[str, str]) -> dict[str, Any]:
        seen.append(params)
        return {"result": [{"update_id": 5, "message": {"chat": {"id": 1}, "text": "x"}}]}

    fetch = telegram_fetcher("tok", get=fake_get)
    out = fetch(99)
    assert out[0].update_id == 5
    assert seen[0]["offset"] == "99" and "timeout" in seen[0]
