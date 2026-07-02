"""Tests for worktree name derivation and the sandboxed test runner."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from stromboli.integrations.notion import Repo
from stromboli.sandbox.runner import (
    DEFAULT_SANDBOX_IMAGE,
    SandboxRunner,
    WorktreeManager,
    clone_url,
    derive_branch_name,
    slugify,
)


def test_worktree_manager_ensure_is_durable_and_idempotent(tmp_path: Path) -> None:
    cmds: list[list[str]] = []
    mgr = WorktreeManager(tmp_path, run=lambda argv: cmds.append(list(argv)))
    repo = Repo(owner="o", repo="r")

    wt = mgr.ensure(repo, "t1", "Add flag")
    assert any("add" in c and "-B" in c for c in cmds)  # first call provisions

    # Simulate the worktree now existing on disk (a git worktree has a .git file).
    wt.path.mkdir(parents=True, exist_ok=True)
    (wt.path / ".git").write_text("gitdir: ...")
    cmds.clear()

    again = mgr.ensure(repo, "t1", "Add flag")
    assert again.path == wt.path
    assert not any("add" in c for c in cmds)  # reused — NOT reset (keeps changes)

    cmds.clear()
    mgr.remove(repo, "t1", "Add flag")
    assert any("remove" in c for c in cmds)


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
    calls: list[list[str]] = []

    def fake_run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        calls.append(list(argv))
        return 0, "1 passed"

    runner = SandboxRunner(run=fake_run, use_docker=True)
    result = runner.run_tests("/tmp/wt", ("pytest", "-q"))
    assert result.passed is True
    assert result.exit_code == 0
    # A stale container is cleared first, then a named/labelled (visible) run.
    assert calls[0][:3] == ["docker", "rm", "-f"]
    argv = calls[-1]
    assert argv[:2] == ["docker", "run"]
    assert "--name" in argv and "--label" in argv
    assert "--rm" not in argv  # kept for inspection (docker ps -a)
    assert "none" in argv  # --network none
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


def test_no_tests_collected_is_not_a_failure() -> None:
    # pytest exit 5 = no tests collected → not a failure of the change itself.
    runner = SandboxRunner(run=lambda _a, _c: (5, ""), use_docker=False)
    result = runner.run_tests("/tmp/wt")
    assert result.passed is True
    assert result.exit_code == 5
    assert "no tests collected" in result.output


def test_sandbox_no_docker_runs_command_directly() -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        seen["argv"] = list(argv)
        return 0, ""

    SandboxRunner(run=fake_run, use_docker=False).run_tests("/tmp/wt", ("make", "test"))
    assert seen["argv"] == ["make", "test"]


def test_clone_url_prefers_explicit_source() -> None:
    # A CLI task's local-path repo clones from the path — no token, no github.
    repo = Repo(owner="local", repo="scratch", source="/tmp/scratch")
    assert clone_url(repo, token="secret") == "/tmp/scratch"
    # Without a source the GitHub HTTPS URL (with token) is unchanged.
    gh = Repo(owner="o", repo="r")
    assert clone_url(gh, token="secret") == "https://x-access-token:secret@github.com/o/r.git"


def test_ensure_clone_seeds_build_junk_excludes(tmp_path: Path) -> None:
    cmds: list[list[str]] = []
    manager = WorktreeManager(tmp_path, run=lambda a: cmds.append(list(a)))
    repo = Repo(owner="o", repo="r")
    clone = manager.ensure_clone(repo)
    exclude = (clone / ".git" / "info" / "exclude").read_text()
    # Junk must never reach the diff / the PR commit (`git add -A`).
    for pattern in ("__pycache__/", ".stromboli-venv/"):
        assert pattern in exclude
    # Idempotent: a second call doesn't duplicate the block.
    (clone / ".git").mkdir(exist_ok=True)
    manager.ensure_clone(repo)
    assert exclude.count("__pycache__/") == 1


def test_sandbox_bootstraps_deps_when_manifest_exists(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def recorder(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        calls.append(list(argv))
        return 0, "ok"

    (tmp_path / "requirements.txt").write_text("httpx\n")
    runner = SandboxRunner(run=recorder)
    result = runner.run_tests(tmp_path)
    assert result.passed
    # First call: the networked prep container installing into the venv.
    prep = calls[0]
    assert prep[:2] == ["docker", "run"] and "--network" not in prep
    assert "uv venv .stromboli-venv" in prep[-1]
    assert "-r requirements.txt" in prep[-1]
    # The test run itself uses the venv python — offline.
    test_run = calls[-1]
    assert "--network" in test_run and "none" in test_run
    assert ".stromboli-venv/bin/python" in test_run


def test_sandbox_skips_bootstrap_without_manifest(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def recorder(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        calls.append(list(argv))
        return 0, "ok"

    runner = SandboxRunner(run=recorder)
    runner.run_tests(tmp_path)
    assert not any("uv venv" in " ".join(c) for c in calls)


def test_sandbox_falls_back_when_bootstrap_fails(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def recorder(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        calls.append(list(argv))
        if "-prep" in " ".join(argv):
            return 1, "resolution failed"
        return 0, "ok"

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    runner = SandboxRunner(run=recorder)
    result = runner.run_tests(tmp_path)
    # Install failure degrades to the bare image python (the old behavior).
    assert result.passed
    assert "python" in calls[-1] and ".stromboli-venv/bin/python" not in calls[-1]
