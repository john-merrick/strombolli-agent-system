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

logger = logging.getLogger(__name__)

#: Sends one message text to a Telegram chat.
Sender = Callable[[str], None]
#: Posts ``payload`` to ``url`` (the Telegram Bot API); injected for tests.
TelegramPoster = Callable[[str, dict[str, str]], None]

#: Timeout, in seconds, for a Telegram API call.
TELEGRAM_TIMEOUT = 10.0


def _httpx_post(url: str, payload: dict[str, str]) -> None:
    """Default Telegram poster: a best-effort HTTP POST via httpx."""
    import httpx

    response = httpx.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
    response.raise_for_status()


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
    "TelegramNotifier",
    "TelegramPoster",
    "make_notifier",
    "telegram_sender",
]
