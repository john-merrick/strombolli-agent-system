"""Telegram push notifications — the "tell me without looking" signal (PRD §2).

A thin, best-effort Telegram Bot API client used by the Human Interrupt node
(escalations / ambiguous specs) and at terminal state (done / escalated). Every
send is best-effort: a Telegram outage logs a warning and is swallowed, never
disturbing a run. Decoupled from any queue/ledger — it takes plain strings.

Set ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` to enable; leave either unset
and :func:`make_notifier` returns a no-op notifier.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Sends one message text to a Telegram chat.
Sender = Callable[[str], None]
#: Posts ``payload`` to ``url`` (the Telegram Bot API); injected for tests.
TelegramPoster = Callable[[str, dict[str, str]], None]
#: Fetches Telegram updates since an offset (long-poll); injected for tests.
TelegramFetcher = Callable[[int | None], list["Update"]]

#: Timeout, in seconds, for a Telegram API call.
TELEGRAM_TIMEOUT = 10.0
#: Long-poll hold time (seconds) for getUpdates — the server holds the request
#: open until a message arrives or this elapses, so the receiver isn't a busy loop.
TELEGRAM_LONG_POLL = 25


def _httpx_post(url: str, payload: dict[str, str]) -> None:
    """Default Telegram poster: a best-effort HTTP POST via httpx."""
    import httpx

    response = httpx.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
    response.raise_for_status()


@dataclass(frozen=True)
class Update:
    """One inbound Telegram message (the slice the investigate loop needs)."""

    update_id: int
    chat_id: str
    text: str


def parse_updates(payload: dict[str, Any]) -> list[Update]:
    """Extract text messages from a getUpdates response (skips non-text updates)."""
    updates: list[Update] = []
    for item in payload.get("result", []):
        message = item.get("message") or item.get("edited_message") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        if text is None or "id" not in chat:
            continue
        updates.append(
            Update(update_id=int(item["update_id"]), chat_id=str(chat["id"]), text=text)
        )
    return updates


def _httpx_get(url: str, params: dict[str, str]) -> dict[str, Any]:
    """Default Telegram getter: a GET via httpx (timeout > long-poll hold)."""
    import httpx

    response = httpx.get(
        url, params=params, timeout=TELEGRAM_LONG_POLL + TELEGRAM_TIMEOUT
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def telegram_fetcher(
    bot_token: str,
    *,
    get: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
) -> TelegramFetcher:
    """Build a :data:`TelegramFetcher` that long-polls the Bot API for messages."""
    getter = get or _httpx_get
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    def fetch(offset: int | None) -> list[Update]:
        params: dict[str, str] = {"timeout": str(TELEGRAM_LONG_POLL)}
        if offset is not None:
            params["offset"] = str(offset)
        return parse_updates(getter(url, params))

    return fetch


def telegram_sender(
    bot_token: str, chat_id: str, *, post: TelegramPoster | None = None
) -> Sender:
    """Build a :data:`Sender` that posts to the Telegram Bot API."""
    poster = post or _httpx_post
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(text: str) -> None:
        poster(url, {"chat_id": chat_id, "text": text})

    return send


class Notifier:
    """A best-effort Telegram notifier. Base is a no-op (notifications disabled)."""

    def notify(self, text: str) -> None:
        """Send ``text``; the base notifier does nothing."""

    def escalation(self, task_id: str, reason: str) -> None:
        """Push a human-attention escalation (PRD §6.8)."""
        self.notify(f"🚨 Escalation: {task_id}\n{reason}")

    def done(self, task_id: str, pr_url: str | None) -> None:
        """Push a terminal success (PRD §6.7)."""
        tail = f"\n{pr_url}" if pr_url else ""
        self.notify(f"✅ Done: {task_id}{tail}")


class NullNotifier(Notifier):
    """The default notifier: does nothing. Used when Telegram is not configured."""


@dataclass
class TelegramNotifier(Notifier):
    """Pushes messages to Telegram; every send is swallowed on failure."""

    send: Sender

    def notify(self, text: str) -> None:
        try:
            self.send(text)
        except Exception:  # noqa: BLE001 - a notification must never break a run
            logger.warning("Telegram notification failed; ignoring.", exc_info=True)


def make_notifier(
    bot_token: str | None,
    chat_id: str | None,
    *,
    post: TelegramPoster | None = None,
) -> Notifier:
    """Construct a :class:`TelegramNotifier`, or a no-op when unconfigured."""
    if not (bot_token and chat_id):
        return NullNotifier()
    return TelegramNotifier(send=telegram_sender(bot_token, chat_id, post=post))


__all__ = [
    "Notifier",
    "NullNotifier",
    "Sender",
    "TelegramFetcher",
    "TelegramNotifier",
    "TelegramPoster",
    "Update",
    "make_notifier",
    "parse_updates",
    "telegram_fetcher",
    "telegram_sender",
]
