"""The investigate-serve service — the bidirectional Telegram resolver for suspended tasks.

A standalone long-poll loop over the dedicated ``strombolli-investigate-bot``
(design DL-6/DL-9). It authenticates the sender, parses a tiny command grammar,
routes each message to a queued task by its ``#N`` handle, and drives the
resolution conversation:

- ``/queued``            list open queued tasks
- ``#N <message>``       talk to the investigator about task ``#N``
- ``#N ✅`` / ``✅``       apply the agreed guidance and resume task ``#N``
- ``/retest #N``         re-run the task's tests and feed the result into the chat
- ``/drop #N``           give up on ``#N`` → park it to Review

The heavy collaborators are injected seams so this module stays pure and
testable: the **responder** (the Investigator agent, Phase 3), the **resumer**
(the resume path, Phase 4), and the **prober** (sandbox re-test, Phase 3). With
none configured the service still routes, lists, and drops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stromboli.integrations.telegram import Sender, TelegramFetcher, Update
from stromboli.orchestration.paused import PausedIndex, PausedTask

logger = logging.getLogger(__name__)

#: Investigator turn: ``(reply_text, proposed_guidance | None)`` (Phase 3).
Responder = Callable[[PausedTask, str], tuple[str, str | None]]
#: Apply guidance + resume a task; returns a human-readable status (Phase 4).
Resumer = Callable[[PausedTask, str], str]
#: Re-run a task's tests in its worktree; returns a short summary (Phase 3).
Prober = Callable[[PausedTask], str]

_APPROVE_WORDS = {"✅", "👍", "approve", "apply", "go", "yes", "ok"}


@dataclass(frozen=True)
class Command:
    """A parsed inbound command."""

    kind: str  # list | help | talk | approve | drop | retest
    ref: int | None = None
    text: str = ""


def _parse_ref(s: str) -> int | None:
    """Parse a ``#N`` / ``N`` reference from a fragment, or None."""
    cleaned = s.strip().lstrip("#").strip()
    if not cleaned:
        return None
    try:
        return int(cleaned.split()[0])
    except (ValueError, IndexError):
        return None


def _split_ref(raw: str) -> tuple[int | None, str]:
    """Split a leading ``#N`` off ``raw`` → ``(ref, remaining_text)``."""
    parts = raw[1:].split(maxsplit=1)
    head = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    try:
        return int(head), rest
    except ValueError:
        return None, raw  # "#abc" isn't a ref — treat the whole thing as text


def parse_command(text: str) -> Command:
    """Parse one inbound message into a :class:`Command` (pure)."""
    raw = text.strip()
    low = raw.lower()
    if low in {"/queued", "/list", "queued", "/q"}:
        return Command("list")
    if low in {"/help", "help", "/start"}:
        return Command("help")
    if low.startswith("/drop"):
        return Command("drop", ref=_parse_ref(raw[len("/drop"):]))
    if low.startswith("/retest") or low.startswith("/test"):
        frag = raw.split(maxsplit=1)
        return Command("retest", ref=_parse_ref(frag[1]) if len(frag) > 1 else None)

    ref: int | None = None
    body = raw
    if raw.startswith("#"):
        ref, body = _split_ref(raw)
    if body.strip().lower() in _APPROVE_WORDS:
        return Command("approve", ref=ref)
    return Command("talk", ref=ref, text=body.strip())


@dataclass
class InvestigateService:
    """Routes investigate-bot messages to queued tasks and drives the conversation."""

    index: PausedIndex
    send: Sender
    authorized_chat_id: str
    responder: Responder | None = None
    resumer: Resumer | None = None
    prober: Prober | None = None
    notion: Any = None  # for /drop → Review (optional)

    # -- inbound ----------------------------------------------------------- #
    def handle(self, update: Update) -> None:
        """Authenticate, dispatch, and reply to one inbound message."""
        if str(update.chat_id) != str(self.authorized_chat_id):
            logger.warning(
                "Ignoring investigate message from unauthorized chat %s",
                update.chat_id,
            )
            return
        reply = self._dispatch(parse_command(update.text))
        if reply:
            try:
                self.send(reply)
            except Exception:  # noqa: BLE001 - a reply failure must not kill the loop
                logger.warning("Failed to send investigate reply.", exc_info=True)

    def _dispatch(self, cmd: Command) -> str:
        if cmd.kind == "help":
            return _HELP
        if cmd.kind == "list":
            return self._format_queue()
        task, err = self._resolve(cmd.ref)
        if err:
            return err
        assert task is not None
        if cmd.kind == "talk":
            return self._talk(task, cmd.text)
        if cmd.kind == "approve":
            return self._approve(task)
        if cmd.kind == "retest":
            return self._retest(task)
        if cmd.kind == "drop":
            return self._drop(task)
        return _HELP

    # -- actions ----------------------------------------------------------- #
    def _talk(self, task: PausedTask, text: str) -> str:
        self.index.append_message(task.task_id, "human", text)
        if self.responder is None:
            return f"#{task.ref}: noted (investigator not configured)."
        message, guidance = self.responder(task, text)
        self.index.append_message(task.task_id, "investigator", message)
        if guidance:
            self.index.set_guidance(task.task_id, guidance)
            return f"{message}\n\n→ Reply `#{task.ref} ✅` to apply this and resume."
        return message

    def _approve(self, task: PausedTask) -> str:
        fresh = self.index.get(task.task_id) or task
        if not fresh.guidance:
            return f"#{task.ref}: nothing to apply yet — discuss it first."
        if self.resumer is None:
            return f"#{task.ref}: resume not configured."
        return self.resumer(fresh, fresh.guidance)

    def _retest(self, task: PausedTask) -> str:
        if self.prober is None:
            return f"#{task.ref}: probe not configured."
        summary = self.prober(task)
        self.index.append_message(task.task_id, "probe", summary)
        return f"#{task.ref} retest:\n{summary}"

    def _drop(self, task: PausedTask) -> str:
        self.index.resolve(task.task_id, state="expired")
        if self.notion is not None:
            from stromboli.integrations.notion import STATUS_REVIEW

            try:
                self.notion.update_task(task.task_id, status=STATUS_REVIEW)
            except Exception:  # noqa: BLE001 - status write must not crash the loop
                logger.warning("Could not park #%s to Review.", task.ref)
        return f"#{task.ref} dropped → Review."

    # -- routing ----------------------------------------------------------- #
    def _resolve(self, ref: int | None) -> tuple[PausedTask | None, str | None]:
        if ref is not None:
            task = self.index.by_ref(ref)
            return (task, None) if task else (None, f"No open task #{ref}. Try /queued.")
        open_tasks = self.index.open_tasks()
        if not open_tasks:
            return None, "Nothing is queued."
        if len(open_tasks) == 1:
            return open_tasks[0], None
        refs = ", ".join(f"#{t.ref}" for t in open_tasks)
        first = open_tasks[0].ref
        return None, f"Several queued ({refs}). Address one, e.g. `#{first} …`."

    def _format_queue(self) -> str:
        open_tasks = self.index.open_tasks()
        if not open_tasks:
            return "Nothing is queued. 🎉"
        lines = [
            f"#{t.ref} — {t.name or t.task_id} ({t.reason}; paused {t.paused_at})"
            for t in open_tasks
        ]
        return "Queued:\n" + "\n".join(lines)

    # -- the long-poll loop ------------------------------------------------ #
    def serve(
        self,
        fetch: TelegramFetcher,
        *,
        sweep: Callable[[], None] | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> None:
        """Long-poll ``fetch`` for messages, dispatch each, and run ``sweep`` per tick.

        The persisted offset (in the index) means a restart neither reprocesses nor
        drops messages. ``should_continue`` bounds the loop in tests; in production
        it runs forever (the fetch long-polls, so this is not a busy loop).
        """
        keep_going = should_continue or (lambda: True)
        offset = self.index.get_offset()
        while keep_going():
            try:
                updates = fetch(offset)
            except Exception:  # noqa: BLE001 - a fetch error must not kill the loop
                logger.warning("getUpdates failed; retrying.", exc_info=True)
                updates = []
            for update in updates:
                offset = update.update_id + 1
                self.index.set_offset(offset)
                self.handle(update)
            if sweep is not None:
                try:
                    sweep()
                except Exception:  # noqa: BLE001 - sweep error must not kill the loop
                    logger.warning("Expiry sweep failed.", exc_info=True)


def make_prober(
    index: PausedIndex,
    sandbox: Any,
    worktree_for: Any,
    *,
    test_command: Any = None,
) -> Prober:
    """Build the ``/retest`` probe: re-run a suspended task's tests in its worktree.

    Read/execute only — it never edits. The result is fed into the transcript so
    the investigator sees fresh test output on the next turn (design DL-3).
    """
    from stromboli.sandbox.runner import DEFAULT_TEST_COMMAND

    command = test_command or DEFAULT_TEST_COMMAND

    def prober(task: PausedTask) -> str:
        state = index.load_state(task.task_id)
        if state is None:
            return "no saved state to test."
        try:
            worktree = worktree_for(state)
            result = sandbox.run_tests(worktree.path, command)
        except Exception as exc:  # noqa: BLE001 - a probe failure is just reported
            return f"probe failed: {exc}"
        if result.passed:
            return "tests passed ✅"
        tail = result.output[-1200:]
        return f"tests failed (exit {result.exit_code})\n{tail}"

    return prober


def make_resumer(phases: Any, index: PausedIndex, *, notion: Any = None) -> Resumer:
    """Build the ``✅`` resume path: apply guidance and re-run the task in place.

    Loads the suspended state, flips Notion ``Working on``, runs one guided
    coding→verify cycle via the phases, finalizes (Complete or Review), and closes
    the paused row. A second failure lands in Review — never re-queued (DL-13).
    """
    from stromboli.integrations.notion import STATUS_WORKING

    def resumer(task: PausedTask, guidance: str) -> str:
        state = index.load_state(task.task_id)
        if state is None:
            index.resolve(task.task_id, state="expired")
            return f"#{task.ref}: lost the saved state — can't resume (parked)."
        if notion is not None and state.source == "notion":
            try:
                notion.update_task(task.task_id, status=STATUS_WORKING)
            except Exception:  # noqa: BLE001 - status write must not block the resume
                logger.warning("Could not set Working on for #%s.", task.ref)
        final = phases.resume_with_guidance(state, guidance)
        phases.finalize(final)
        index.resolve(task.task_id, state="resumed")
        if final.status == "done":
            tail = f" → {final.pr_url}" if final.pr_url else ""
            return f"#{task.ref} resumed and completed{tail}."
        return f"#{task.ref} still didn't pass after that — parked to Review."

    return resumer


_HELP = (
    "Stromboli investigate loop:\n"
    "• `/queued` — list queued tasks\n"
    "• `#N <message>` — discuss task #N\n"
    "• `#N ✅` — apply the agreed fix and resume #N\n"
    "• `/retest #N` — re-run #N's tests\n"
    "• `/drop #N` — give up on #N (→ Review)"
)


__all__ = [
    "Command",
    "InvestigateService",
    "Prober",
    "Responder",
    "Resumer",
    "make_prober",
    "make_resumer",
    "parse_command",
]
