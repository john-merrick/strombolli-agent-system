"""Phase 6 — a Notion task flows end-to-end to a PR + Telegram + write-back.

Offline analogue of the PRD §10 Phase-6 DoD: a Notion-sourced task runs through
the whole graph with fakes and produces a verified PR, a Notion feedback summary
+ Review status, and a Telegram "done" — no live network, Docker, or LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from stromboli.graph import GraphDeps, run_task
from stromboli.integrations.github import PullRequest
from stromboli.integrations.telegram import TelegramNotifier
from stromboli.llm.coder import CoderRun, TurnRecord
from stromboli.memory import Memory
from stromboli.sandbox.runner import SandboxResult
from tests.memory._fakes import make_store
from tests.nodes._fakes import FakeNotion, RoutingGateway, make_task, make_worktree


class _Coder:
    def run(self, prompt: str, cwd: Any, *, resume: str | None = None) -> CoderRun:
        return CoderRun(
            diff="diff --git a/x b/x\n+ok", final_text="done", turns=2,
            session_id="sess-1", subtype="success", is_error=False, cost_usd=0.02,
            usage=None, turn_records=(TurnRecord(index=1, tools=("Edit",), usage=None),),
        )


class _Sandbox:
    def run_tests(self, worktree_path: Any, command: Sequence[str] = ()) -> SandboxResult:
        return SandboxResult(passed=True, output="2 passed", exit_code=0)


class _GitHub:
    def open_pull_request(
        self, repo: Any, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        return PullRequest(url="https://github.com/o/r/pull/9", number=9)


def _dirty_git(args: Sequence[str]) -> str:
    return " M x\n" if "status" in args else "1"


def test_notion_task_reaches_verified_pr_with_writeback() -> None:
    pushes: list[str] = []
    notion = FakeNotion(make_task(page_id="pg-1", spec="add a --verbose flag"))
    deps = GraphDeps(
        gateway=RoutingGateway(
            {
                "Spec": {"goal": "add --verbose", "acceptance_criteria": ["prints debug"],
                         "ambiguous": False},
                "Verdict": {"decision": "pass", "reason": "meets criteria",
                            "coverage_note": "tests cover the flag"},
            }
        ),
        reasoning_model="haiku",
        verifier_model="gemini/gemini-2.5-pro",
        coder=_Coder(),
        sandbox=_Sandbox(),
        worktree_for=lambda _s: make_worktree(),
        memory=Memory(make_store()),
        github=_GitHub(),
        notion=notion,
        notifier=TelegramNotifier(send=pushes.append),
        git_run=_dirty_git,
        dry_run_pr=False,
    )

    final = run_task(
        "", source="notion", task_id="pg-1", deps=deps, checkpointer=MemorySaver()
    )

    # Verified PR opened end-to-end.
    assert final.status == "done"
    assert final.pr_url == "https://github.com/o/r/pull/9"
    # Notion: PR url written back, status routed to Review (no auto-merge),
    # and a feedback summary appended.
    assert notion.pr_writes == [("pg-1", "https://github.com/o/r/pull/9")]
    assert ("pg-1", "Review") in notion.status_writes
    assert any("build summary" in md for _pid, md in notion.appended)
    # Telegram "done" was pushed.
    assert any("Done" in p for p in pushes)
    # The episodic trace was deposited (the loop learns).
    assert final.memory_refs


def test_cli_source_uses_raw_request_directly() -> None:
    # A CLI task with no repo/worktree runs the coding stub but still completes.
    deps = GraphDeps()  # all stubs
    final = run_task("do a thing", source="cli", deps=deps)
    assert final.status == "done"
