"""Tests for the Notion acknowledgment listener."""

from __future__ import annotations

import pytest

from stromboli.ledger import RunRecord, RunState
from stromboli.notify import (
    NotionAck,
    TelegramNotifier,
    make_telegram_notifier,
    render_building,
    render_queued,
    telegram_finished,
    telegram_queued,
    telegram_sender,
)


def _run(**overrides: object) -> RunRecord:
    base: dict[str, object] = {
        "id": 7,
        "page_id": "page-1",
        "task_name": "Add healthcheck",
        "engine": "graph",
        "state": RunState.QUEUED,
        "stage": None,
        "outcome": None,
        "error": None,
        "queued_at": "2026-06-29T12:00:00+00:00",
        "started_at": None,
        "ended_at": None,
        "heartbeat_at": None,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


class FakeNotion:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str]] = []

    def append_task_body(self, page_id: str, markdown: str) -> None:
        self.appended.append((page_id, markdown))


def test_render_queued_shows_next_up_for_head() -> None:
    note = render_queued(_run(), position=0)
    assert "next up" in note
    assert "2026-06-29T12:00:00+00:00" in note


def test_render_queued_shows_place_in_line() -> None:
    note = render_queued(_run(), position=2)
    assert "#3 in the queue" in note


def test_render_building_names_the_run() -> None:
    assert "run 7" in render_building(_run())


def test_ack_appends_queued_note_with_position() -> None:
    notion = FakeNotion()
    NotionAck(notion).queued(_run(), position=1)
    assert len(notion.appended) == 1
    page_id, note = notion.appended[0]
    assert page_id == "page-1"
    assert "#2 in the queue" in note


def test_ack_appends_building_note() -> None:
    notion = FakeNotion()
    NotionAck(notion).building(_run())
    assert notion.appended[0][0] == "page-1"
    assert "building" in notion.appended[0][1].lower()


def test_ack_finished_is_a_noop_to_avoid_clutter() -> None:
    notion = FakeNotion()
    NotionAck(notion).finished(_run(state=RunState.DONE))
    assert notion.appended == []


def test_ack_never_raises_on_notion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stromboli.writeback.time.sleep", lambda _: None)

    class BrokenNotion:
        def append_task_body(self, page_id: str, markdown: str) -> None:
            raise RuntimeError("notion down")

    # resilient_append swallows the failure — the ack must not propagate it.
    NotionAck(BrokenNotion()).queued(_run(), position=0)


# --------------------------------------------------------------------------- #
# Telegram notifier                                                           #
# --------------------------------------------------------------------------- #
def test_telegram_finished_messages_per_state() -> None:
    assert telegram_finished(_run(state=RunState.DONE)).startswith("✅ Done")
    failed = telegram_finished(_run(state=RunState.FAILED, error="boom"))
    assert failed.startswith("❌ Failed")
    assert "boom" in failed
    skipped = telegram_finished(_run(state=RunState.SKIPPED, outcome="not_ready"))
    assert skipped.startswith("⏭️ Skipped")
    assert "not_ready" in skipped


def test_telegram_uses_task_name_when_present() -> None:
    assert "Add healthcheck" in telegram_queued(_run(), position=0)


def test_notifier_sends_on_each_lifecycle_event() -> None:
    sent: list[str] = []
    notifier = TelegramNotifier(send=sent.append)
    run = _run()
    notifier.queued(run, 1)
    notifier.building(run)
    notifier.finished(_run(state=RunState.DONE))
    assert len(sent) == 3
    assert sent[0].startswith("🍕 Queued")
    assert sent[1].startswith("🛠️ Building")
    assert sent[2].startswith("✅ Done")


def test_notifier_swallows_send_failures() -> None:
    def boom(text: str) -> None:
        raise RuntimeError("telegram down")

    # Must not raise into the consumer.
    TelegramNotifier(send=boom).finished(_run(state=RunState.FAILED))


def test_sender_posts_to_the_bot_api_with_chat_id() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_post(url: str, payload: dict[str, str]) -> None:
        calls.append((url, payload))

    send = telegram_sender("BOT-TOKEN", "CHAT-42", post=fake_post)
    send("hello")
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.telegram.org/botBOT-TOKEN/sendMessage"
    assert payload == {"chat_id": "CHAT-42", "text": "hello"}


def test_make_telegram_notifier_wires_token_and_chat() -> None:
    calls: list[dict[str, str]] = []
    notifier = make_telegram_notifier(
        "T", "C", post=lambda url, payload: calls.append(payload)
    )
    notifier.building(_run())
    assert calls[0]["chat_id"] == "C"
    assert "Building" in calls[0]["text"]
