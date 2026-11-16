"""The CLI table and the HTML report."""

from __future__ import annotations

import pytest

from meridian.models.run import (
    ErrorCause,
    Outcome,
    RunResult,
    RunStatus,
    TaskResult,
    TrialResult,
)
from meridian.report import html as html_report
from meridian.report import table

pytestmark = pytest.mark.unit


def build_run(**overrides: object) -> RunResult:
    def trials(slug: str, passes: int, total: int) -> tuple[TrialResult, ...]:
        return tuple(
            TrialResult(
                run_id="r",
                task_slug=slug,
                trial_index=i,
                seed=i,
                outcome=Outcome.PASS if i < passes else Outcome.FAIL,
                detail="" if i < passes else "$.total_cents is 3999, expected 4999",
            )
            for i in range(total)
        )

    payload: dict[str, object] = {
        "run_id": "run-xyz",
        "suite_slug": "checkout-agent",
        "suite_version": 1,
        "status": RunStatus.COMPLETE,
        "k": 3,
        "n_requested": 5,
        "tasks": (
            TaskResult(task_slug="happy-path", trials=trials("happy-path", 4, 5)),
            TaskResult(task_slug="missing-field", trials=trials("missing-field", 5, 5)),
        ),
        "excluded_task_slugs": ("contamination-probe",),
        "cost_cents": 51,
        "duration_ms": 34_900,
    }
    return RunResult.model_validate({**payload, **overrides})


def test_the_table_puts_pass_at_k_beside_pass_hat_k() -> None:
    """The headline comparison, on one row: 1.00 next to 0.40."""
    rendered = table.render(build_run())
    line = next(row for row in rendered.splitlines() if row.startswith("happy-path"))
    assert "1.00" in line and "0.40" in line


def test_the_table_shows_a_wilson_interval_for_each_task() -> None:
    """The per-task pass rate is a small binomial sample; say how uncertain it is."""
    rendered = table.render(build_run())
    rows = {
        row.split()[0]: row
        for row in rendered.splitlines()
        if row.startswith(("happy-path", "missing-field"))
    }
    assert "[0.38, 0.96]" in rows["happy-path"]  # 4 of 5
    assert "[0.57, 1.00]" in rows["missing-field"]  # 5 of 5


def test_the_table_reports_the_interval_and_the_exclusions() -> None:
    rendered = table.render(build_run())
    assert "95% CI (over tasks)" in rendered
    assert "contamination-probe" in rendered


def test_a_task_with_no_gradeable_trials_says_so_rather_than_scoring_zero() -> None:
    """A trial Meridian could not run is not evidence about the agent."""
    run = build_run(
        tasks=(
            TaskResult(
                task_slug="broken",
                trials=(
                    TrialResult(
                        run_id="r",
                        task_slug="broken",
                        trial_index=0,
                        seed=0,
                        outcome=Outcome.HARNESS_ERROR,
                        error_cause=ErrorCause.INTERNAL,
                        detail="docker went away",
                    ),
                ),
            ),
        )
    )
    assert "no gradeable trials" in table.render(run)


def test_the_html_report_is_self_contained() -> None:
    """A report that needs the network cannot be opened from a CI artifact."""
    body = html_report.render(build_run(), manifest_hash="sha256:" + "ab" * 32)
    assert "<script" not in body
    assert "http://" not in body and "https://" not in body
    assert "@import" not in body


def test_the_html_report_names_the_unreliable_and_never_passing_tasks() -> None:
    run = build_run(
        tasks=(
            TaskResult(
                task_slug="never",
                trials=tuple(
                    TrialResult(
                        run_id="r", task_slug="never", trial_index=i, seed=i, outcome=Outcome.FAIL
                    )
                    for i in range(5)
                ),
            ),
            TaskResult(
                task_slug="flaky",
                trials=tuple(
                    TrialResult(
                        run_id="r",
                        task_slug="flaky",
                        trial_index=i,
                        seed=i,
                        outcome=Outcome.PASS if i < 4 else Outcome.FAIL,
                    )
                    for i in range(5)
                ),
            ),
        )
    )
    body = html_report.render(run)
    assert "never passes" in body
    assert "unreliable" in body


def test_a_halted_run_is_marked_partial(html_ok: None = None) -> None:
    body = html_report.render(build_run(status=RunStatus.HALTED_BUDGET))
    assert "halted at its cost cap" in body


def test_task_names_are_escaped() -> None:
    """Task slugs are author-supplied; a report is not a place to trust them."""
    run = build_run(
        tasks=(
            TaskResult(
                task_slug="a<script>",
                trials=(
                    TrialResult(
                        run_id="r",
                        task_slug="a<script>",
                        trial_index=0,
                        seed=0,
                        outcome=Outcome.PASS,
                    ),
                ),
            ),
        )
    )
    body = html_report.render(run)
    assert "a&lt;script&gt;" in body
    assert "<script>" not in body
