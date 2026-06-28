"""Notion acknowledgment — tell the task it was picked up, the moment it is.

The gap this closes: after ticking *Ready* you had no confirmation Stromboli
ever received the dispatch. :class:`NotionAck` is a lifecycle listener that
appends a concise note to the task as soon as it is **queued** (with its place
in line) and again when it starts **building** — so the Notion page you already
live in answers "did it kick off / is it running" without checking logs.

The *finished* outcome is intentionally a no-op here: the build pipeline already
writes a detailed feedback summary back to the task, so acking it again would
just be clutter. (A future Telegram notifier is a separate listener that *will*
care about ``finished``.) Appends are best-effort via
:func:`~stromboli.writeback.resilient_append`, so a Notion hiccup never disturbs
the build.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from stromboli.ledger import RunRecord, RunState
from stromboli.writeback import AppendGateway, resilient_append

logger = logging.getLogger(__name__)


def render_queued(run: RunRecord, position: int) -> str:
    """The note appended when a dispatch is accepted into the queue."""
    where = "next up" if position == 0 else f"#{position + 1} in the queue"
    return f"🍕 **Stromboli queued** this task ({where}) at {run.queued_at}."


def render_building(run: RunRecord) -> str:
    """The note appended when the build actually starts."""
    return f"🛠️ **Stromboli is building** this task (run {run.id})."


@dataclass
class NotionAck:
    """Lifecycle listener that acks queued / building back to the Notion task."""

    notion: AppendGateway

    def queued(self, run: RunRecord, position: int) -> None:
        resilient_append(self.notion, run.page_id, render_queued(run, position))

    def building(self, run: RunRecord) -> None:
        resilient_append(self.notion, run.page_id, render_building(run))

    def finished(self, run: RunRecord) -> None:
        # The build pipeline already writes the outcome summary; don't double up.
        return


# --------------------------------------------------------------------------- #
# Telegram push notifications                                                  #
# --------------------------------------------------------------------------- #
#: Sends one message text to a Telegram chat.
Sender = Callable[[str], None]
#: Posts ``payload`` to ``url`` (the Telegram Bot API); injected for tests.
TelegramPoster = Callable[[str, dict[str, str]], None]

#: Timeout, in seconds, for a Telegram API call.
TELEGRAM_TIMEOUT = 10.0


def _label(run: RunRecord) -> str:
    return run.task_name or run.page_id


def telegram_queued(run: RunRecord, position: int) -> str:
    """The push sent when a dispatch is accepted into the queue."""
    where = "next up" if position == 0 else f"#{position + 1} in queue"
    return f"🍕 Queued: {_label(run)} ({where})"


def telegram_building(run: RunRecord) -> str:
    """The push sent when a build starts."""
    return f"🛠️ Building: {_label(run)}"


def telegram_finished(run: RunRecord) -> str:
    """The push sent when a build finishes — done / failed / skipped."""
    label = _label(run)
    if run.state is RunState.DONE:
        return f"✅ Done: {label}"
    if run.state is RunState.FAILED:
        error = f"\n{run.error}" if run.error else ""
        return f"❌ Failed: {label}{error}"
    return f"⏭️ Skipped: {label} ({run.outcome})"


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


@dataclass
class TelegramNotifier:
    """Lifecycle listener that pushes queued / building / finished to Telegram.

    Unlike :class:`NotionAck`, this *does* notify on ``finished`` — that "✅ done /
    ❌ failed" ping is the whole point of a phone notification. Every send is
    best-effort: a Telegram outage logs a warning and is swallowed, never
    disturbing the build.
    """

    send: Sender

    def queued(self, run: RunRecord, position: int) -> None:
        self._safe(telegram_queued(run, position))

    def building(self, run: RunRecord) -> None:
        self._safe(telegram_building(run))

    def finished(self, run: RunRecord) -> None:
        self._safe(telegram_finished(run))

    def _safe(self, text: str) -> None:
        try:
            self.send(text)
        except Exception:  # noqa: BLE001 - a notification must never break a build
            logger.warning("Telegram notification failed; ignoring.", exc_info=True)


def make_telegram_notifier(
    bot_token: str, chat_id: str, *, post: TelegramPoster | None = None
) -> TelegramNotifier:
    """Construct a :class:`TelegramNotifier` from a bot token + chat id."""
    return TelegramNotifier(send=telegram_sender(bot_token, chat_id, post=post))


__all__ = [
    "NotionAck",
    "Sender",
    "TelegramNotifier",
    "TelegramPoster",
    "make_telegram_notifier",
    "render_building",
    "render_queued",
    "telegram_building",
    "telegram_finished",
    "telegram_queued",
    "telegram_sender",
]
