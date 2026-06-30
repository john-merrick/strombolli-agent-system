"""Tests for the investigate-serve command grammar, routing, auth, and loop."""

from __future__ import annotations

from pathlib import Path

from stromboli.graph import GraphDeps
from stromboli.integrations.telegram import TelegramNotifier, Update
from stromboli.orchestration.investigate import (
    InvestigateService,
    Prober,
    Responder,
    Resumer,
    make_prober,
    make_resumer,
    make_sweeper,
    parse_command,
)
from stromboli.orchestration.paused import PausedIndex, PausedTask
from stromboli.orchestration.phases import TriagePhases
from stromboli.sandbox.runner import SandboxResult
from stromboli.state import StromboliState
from tests.nodes._fakes import FakeNotion, RoutingGateway, make_worktree


def _state(task_id: str = "t1") -> StromboliState:
    return StromboliState(task_id=task_id, source="notion", raw_request="do x")


def _service(
    index: PausedIndex,
    sent: list[str],
    *,
    responder: Responder | None = None,
    resumer: Resumer | None = None,
    prober: Prober | None = None,
    notion: object = None,
) -> InvestigateService:
    return InvestigateService(
        index=index, send=sent.append, authorized_chat_id="42",
        responder=responder, resumer=resumer, prober=prober, notion=notion,
    )


# -- command grammar -------------------------------------------------------- #
def test_parse_command_grammar() -> None:
    assert parse_command("/queued").kind == "list"
    assert parse_command("/help").kind == "help"
    drop = parse_command("/drop #3")
    assert (drop.kind, drop.ref) == ("drop", 3)
    talk = parse_command("#2 the bug is in foo")
    assert (talk.kind, talk.ref, talk.text) == ("talk", 2, "the bug is in foo")
    approve = parse_command("#2 ✅")
    assert (approve.kind, approve.ref) == ("approve", 2)
    bare = parse_command("✅")
    assert (bare.kind, bare.ref) == ("approve", None)
    retest = parse_command("/retest #1")
    assert (retest.kind, retest.ref) == ("retest", 1)


# -- auth ------------------------------------------------------------------- #
def test_unauthorized_chat_is_ignored(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    sent: list[str] = []
    _service(idx, sent).handle(Update(update_id=1, chat_id="999", text="/queued"))
    assert sent == []  # no reply to a stranger


# -- routing ---------------------------------------------------------------- #
def test_queued_listing_and_single_open_inference(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="verifier rejected", name="Add flag")
    sent: list[str] = []
    captured: list[tuple[str, str]] = []

    def responder(task: PausedTask, text: str) -> tuple[str, str | None]:
        captured.append((task.task_id, text))
        return "ok", None

    svc = _service(idx, sent, responder=responder)
    svc.handle(Update(update_id=1, chat_id="42", text="/queued"))
    assert "#1" in sent[-1] and "Add flag" in sent[-1]

    svc.handle(Update(update_id=2, chat_id="42", text="the bug is X"))
    assert captured == [("a", "the bug is X")]


def test_multi_open_requires_ref(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    idx.suspend(_state("b"), reason="r")
    sent: list[str] = []
    _service(idx, sent).handle(Update(update_id=1, chat_id="42", text="hello"))
    assert "Several queued" in sent[-1]


def test_unknown_ref_errors(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    sent: list[str] = []
    _service(idx, sent).handle(Update(update_id=1, chat_id="42", text="#9 hi"))
    assert "No open task #9" in sent[-1]


# -- talk → guidance → approve → resume ------------------------------------- #
def test_talk_stores_guidance_and_approve_resumes(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    sent: list[str] = []
    resumed: list[tuple[str, str]] = []

    def responder(_task: PausedTask, _text: str) -> tuple[str, str | None]:
        return "Try using X.", "Use library X in foo.py"

    def resumer(task: PausedTask, guidance: str) -> str:
        resumed.append((task.task_id, guidance))
        return f"#{task.ref} resuming."

    svc = _service(idx, sent, responder=responder, resumer=resumer)
    svc.handle(Update(update_id=1, chat_id="42", text="#1 what's wrong?"))
    assert "Try using X." in sent[-1] and "✅" in sent[-1]
    row = idx.get("a")
    assert row is not None and row.guidance == "Use library X in foo.py"
    assert idx.transcript("a")[0]["role"] == "human"

    svc.handle(Update(update_id=2, chat_id="42", text="#1 ✅"))
    assert resumed == [("a", "Use library X in foo.py")]


def test_approve_without_guidance_is_guarded(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    sent: list[str] = []

    def resumer(_task: PausedTask, _g: str) -> str:
        return "should not happen"

    _service(idx, sent, resumer=resumer).handle(
        Update(update_id=1, chat_id="42", text="#1 ✅")
    )
    assert "nothing to apply" in sent[-1].lower()


def test_drop_parks_to_review(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")

    class _Notion:
        def __init__(self) -> None:
            self.writes: list[tuple[str, str | None]] = []

        def update_task(self, page_id: str, *, status: str | None = None) -> None:
            self.writes.append((page_id, status))

    notion = _Notion()
    sent: list[str] = []
    _service(idx, sent, notion=notion).handle(
        Update(update_id=1, chat_id="42", text="/drop #1")
    )
    assert ("a", "Review") in notion.writes
    assert idx.by_ref(1) is None
    assert "dropped" in sent[-1].lower()


# -- probe (/retest) -------------------------------------------------------- #
class _FakeSandbox:
    def __init__(self, result: SandboxResult) -> None:
        self._result = result

    def run_tests(self, _path: object, _command: object) -> SandboxResult:
        return self._result


def test_make_prober_reports_failure(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    task = idx.suspend(_state("a"), reason="r")
    sandbox = _FakeSandbox(SandboxResult(passed=False, output="E assert x", exit_code=1))
    prober = make_prober(idx, sandbox, lambda _s: make_worktree())
    out = prober(task)
    assert "tests failed" in out and "assert x" in out


def test_retest_command_invokes_prober_and_records(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    sent: list[str] = []
    svc = _service(idx, sent, prober=lambda _t: "tests passed ✅")
    svc.handle(Update(update_id=1, chat_id="42", text="/retest #1"))
    assert "tests passed" in sent[-1]
    assert any(m["role"] == "probe" for m in idx.transcript("a"))


# -- resume (✅) ------------------------------------------------------------ #
def _queued(idx: PausedIndex, task_id: str = "a") -> None:
    idx.suspend(
        _state(task_id).model_copy(update={"status": "queued"}), reason="r"
    )


def test_make_resumer_completes_and_closes(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    _queued(idx)
    notion = FakeNotion()
    pushes: list[str] = []
    phases = TriagePhases(  # stub coding/verify → happy path → done
        GraphDeps(notion=notion, notifier=TelegramNotifier(send=pushes.append))
    )
    resumer = make_resumer(phases, idx, notion=notion)
    task = idx.get("a")
    assert task is not None
    msg = resumer(task, "use X")
    assert "completed" in msg.lower()
    assert idx.by_ref(1) is None  # paused row closed
    assert ("a", "Working on") in notion.status_writes
    assert ("a", "Complete") in notion.status_writes


def test_make_resumer_failure_parks_to_review(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    _queued(idx)
    notion = FakeNotion()
    gw = RoutingGateway({"Verdict": {"decision": "revise", "reason": "nope"}})
    phases = TriagePhases(GraphDeps(gateway=gw, verifier_model="g", notion=notion))
    resumer = make_resumer(phases, idx, notion=notion)
    task = idx.get("a")
    assert task is not None
    msg = resumer(task, "use X")
    assert "review" in msg.lower()
    assert ("a", "Review") in notion.status_writes


def test_make_resumer_handles_lost_state(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    phases = TriagePhases(GraphDeps())
    resumer = make_resumer(phases, idx)
    ghost = PausedTask(task_id="ghost", ref=9, reason="r", paused_at="x", state="open")
    assert "can't resume" in resumer(ghost, "use X").lower()


# -- expiry sweeper --------------------------------------------------------- #
def test_sweeper_parks_expired_tasks(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("old"), reason="r", name="Old", paused_at="2020-01-01T00:00:00+00:00")
    idx.suspend(_state("new"), reason="r", name="New", paused_at="2999-01-01T00:00:00+00:00")

    class _Notion:
        def __init__(self) -> None:
            self.writes: list[tuple[str, str | None]] = []

        def update_task(self, page_id: str, *, status: str | None = None) -> None:
            self.writes.append((page_id, status))

    notion = _Notion()
    pings: list[str] = []
    cleaned: list[str] = []
    sweep = make_sweeper(
        idx, max_age_days=3, notion=notion, notify=pings.append,
        cleanup=lambda t: cleaned.append(t.task_id),
        clock=lambda: datetime(2026, 6, 30, tzinfo=UTC),
    )
    sweep()
    assert idx.by_ref(1) is None  # "old" closed
    assert idx.get("new") is not None and idx.get("new").state == "open"  # type: ignore[union-attr]
    assert ("old", "Review") in notion.writes
    assert cleaned == ["old"]
    assert pings and "expired" in pings[0]


# -- the serve loop --------------------------------------------------------- #
def test_serve_loop_persists_offset_and_runs_sweep(tmp_path: Path) -> None:
    idx = PausedIndex(tmp_path / "p.db")
    idx.suspend(_state("a"), reason="r")
    sent: list[str] = []
    swept: list[int] = []
    batches = [[Update(update_id=7, chat_id="42", text="/queued")], []]

    def fetch(_offset: int | None) -> list[Update]:
        return batches.pop(0) if batches else []

    _service(idx, sent).serve(
        fetch, sweep=lambda: swept.append(1), should_continue=lambda: bool(batches)
    )
    assert idx.get_offset() == 8  # 7 + 1, persisted
    assert swept
