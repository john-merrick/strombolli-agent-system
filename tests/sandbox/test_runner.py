"""Tests for worktree name derivation and the sandboxed test runner."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from stromboli.integrations.notion import Repo
from stromboli.sandbox.runner import (
    DEFAULT_SANDBOX_IMAGE,
    SandboxRunner,
    clone_url,
    derive_branch_name,
    slugify,
)


def test_slugify() -> None:
    assert slugify("Add a --verbose Flag!") == "add-a-verbose-flag"
    assert slugify("***") == ""


def test_derive_branch_name() -> None:
    assert derive_branch_name("123", "Do it") == "stromboli/123-do-it"
    assert derive_branch_name("123", "***") == "stromboli/123"


def test_clone_url_embeds_token() -> None:
    repo = Repo(owner="o", repo="r")
    assert clone_url(repo) == "https://github.com/o/r.git"
    assert "x-access-token:tok@" in clone_url(repo, "tok")


def test_sandbox_run_passes() -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        seen["argv"] = list(argv)
        return 0, "1 passed"

    runner = SandboxRunner(run=fake_run, use_docker=True)
    result = runner.run_tests("/tmp/wt", ("pytest", "-q"))
    assert result.passed is True
    assert result.exit_code == 0
    # The command is wrapped in an isolated docker container.
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert DEFAULT_SANDBOX_IMAGE in argv
    assert "pytest" in argv


def test_sandbox_run_fails_and_truncates() -> None:
    big = "E" * 50_000

    def fake_run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        return 1, big

    runner = SandboxRunner(run=fake_run, use_docker=False)
    result = runner.run_tests("/tmp/wt")
    assert result.passed is False
    assert result.exit_code == 1
    assert "truncated" in result.output
    assert len(result.output) < len(big)


def test_sandbox_no_docker_runs_command_directly() -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        seen["argv"] = list(argv)
        return 0, ""

    SandboxRunner(run=fake_run, use_docker=False).run_tests("/tmp/wt", ("make", "test"))
    assert seen["argv"] == ["make", "test"]
