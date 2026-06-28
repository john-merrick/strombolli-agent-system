"""Tests for the context-scoped stage reporter."""

from __future__ import annotations

from stromboli import reporting


def test_report_stage_is_a_noop_when_unset() -> None:
    # Must not raise when no reporter is installed.
    reporting.report_stage("nothing listening")


def test_using_installs_and_restores_the_reporter() -> None:
    seen: list[str] = []
    with reporting.using(seen.append):
        reporting.report_stage("one")
        reporting.report_stage("two")
    # Outside the block the reporter is gone again.
    reporting.report_stage("ignored")
    assert seen == ["one", "two"]


def test_reporter_failure_is_swallowed() -> None:
    def boom(stage: str) -> None:
        raise RuntimeError("reporter down")

    with reporting.using(boom):
        reporting.report_stage("still fine")  # must not raise


def test_nested_using_restores_the_outer_reporter() -> None:
    outer: list[str] = []
    inner: list[str] = []
    with reporting.using(outer.append):
        reporting.report_stage("a")
        with reporting.using(inner.append):
            reporting.report_stage("b")
        reporting.report_stage("c")
    assert outer == ["a", "c"]
    assert inner == ["b"]
