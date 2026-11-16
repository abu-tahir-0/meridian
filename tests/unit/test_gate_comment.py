"""The PR comment — the only Meridian output most people will ever read."""

from __future__ import annotations

import pytest

from meridian.gate.comment import (
    paired_suite_scores,
    per_task_scores,
    render,
    sensitivity_note,
    stale_cassette_rate,
    suite_score,
)
from meridian.gate.decide import Verdict, flipped_tasks
from meridian.models.run import (
    ErrorCause,
    Outcome,
    RunResult,
    RunStatus,
    TaskResult,
    TrialResult,
)

pytestmark = pytest.mark.unit


def trial(slug: str, index: int, outcome: Outcome, detail: str = "") -> TrialResult:
    return TrialResult(
        run_id="r",
        task_slug=slug,
        trial_index=index,
        seed=index,
        outcome=outcome,
        detail=detail,
    )


def run(**overrides: object) -> RunResult:
    tasks = (
        TaskResult(
            task_slug="expired-coupon",
            trials=tuple(
                trial(
                    "expired-coupon",
                    i,
                    Outcome.FAIL,
                    "/work/out/invoice-1042.json $.total_cents is 3999, expected 4999",
                )
                for i in range(5)
            ),
        ),
        TaskResult(
            task_slug="happy-path",
            trials=tuple(trial("happy-path", i, Outcome.PASS) for i in range(5)),
        ),
    )
    payload: dict[str, object] = {
        "run_id": "run-abc",
        "suite_slug": "checkout-agent",
        "suite_version": 1,
        "status": RunStatus.COMPLETE,
        "k": 3,
        "n_requested": 5,
        "tasks": tasks,
    }
    return RunResult.model_validate({**payload, **overrides})


def test_the_comment_leads_with_the_verdict_and_the_broken_task() -> None:
    head = run()
    baseline = {"expired-coupon": 1.0, "happy-path": 1.0}
    scores = per_task_scores(head)

    body = render(
        verdict=Verdict.FAIL,
        reason="suite pass^3 dropped 0.500, p=0.019",
        flipped=flipped_tasks(baseline, scores),
        head=head,
        baseline_scores=baseline,
        head_scores=scores,
        baseline_suite=1.0,
        head_suite=suite_score(head),
        baseline_run_id="run-base",
        manifest_hash="sha256:" + "ab" * 32,
    )

    assert body.index("❌ **FAIL**") < body.index("Broken by this change")
    assert "`expired-coupon`" in body
    # The grader's own words, because that is what tells a developer what to do.
    assert "3999" in body and "4999" in body
    assert "meridian replay run-abc" in body


def test_the_comment_names_a_missing_baseline_without_alarm() -> None:
    head = run()
    scores = per_task_scores(head)
    body = render(
        verdict=Verdict.PASS,
        reason="no baseline for this commit; recording head as the new baseline",
        flipped=flipped_tasks({}, scores),
        head=head,
        baseline_scores={},
        head_scores=scores,
        baseline_suite=None,
        head_suite=suite_score(head),
        baseline_run_id=None,
    )
    assert "✅ **PASS**" in body
    assert "no baseline" in body
    assert "—" in body  # the empty baseline column


def test_a_credential_in_a_failure_detail_is_redacted() -> None:
    """A comment is public, and a secret is only secret until it is printed once."""
    leaky = run(
        tasks=(
            TaskResult(
                task_slug="leaky",
                trials=(
                    trial("leaky", 0, Outcome.FAIL, "upstream rejected sk-ant-abcdefghijklmnop"),
                ),
            ),
        )
    )
    scores = per_task_scores(leaky)
    body = render(
        verdict=Verdict.FAIL,
        reason="x",
        flipped=flipped_tasks({"leaky": 1.0}, scores),
        head=leaky,
        baseline_scores={"leaky": 1.0},
        head_scores=scores,
        baseline_suite=1.0,
        head_suite=0.0,
        baseline_run_id=None,
    )
    assert "sk-ant-abcdefghijklmnop" not in body
    assert "«redacted»" in body


def test_the_comment_names_the_harness_error_causes() -> None:
    head = run(
        tasks=(
            TaskResult(
                task_slug="flaky",
                trials=(
                    TrialResult(
                        run_id="r",
                        task_slug="flaky",
                        trial_index=0,
                        seed=0,
                        outcome=Outcome.HARNESS_ERROR,
                        detail="provider timed out",
                        error_cause=ErrorCause.MODEL_PROVIDER,
                    ),
                    TrialResult(
                        run_id="r",
                        task_slug="flaky",
                        trial_index=1,
                        seed=1,
                        outcome=Outcome.HARNESS_ERROR,
                        detail="grader raised",
                        error_cause=ErrorCause.GRADER,
                    ),
                    trial("flaky", 2, Outcome.PASS),
                ),
            ),
        )
    )
    scores = per_task_scores(head)

    body = render(
        verdict=Verdict.INCONCLUSIVE,
        reason="x",
        flipped=flipped_tasks({}, scores),
        head=head,
        baseline_scores={},
        head_scores=scores,
        baseline_suite=None,
        head_suite=suite_score(head),
        baseline_run_id=None,
    )

    assert "harness errors 66.7% (grader: 1, model_provider: 1)" in body


def test_task_set_changes_appear_in_the_comment() -> None:
    head = run()
    scores = per_task_scores(head)
    baseline = {**scores, "withdrawn-task": 1.0}
    body = render(
        verdict=Verdict.PASS,
        reason="x",
        flipped=flipped_tasks(baseline, scores),
        head=head,
        baseline_scores=baseline,
        head_scores=scores,
        baseline_suite=1.0,
        head_suite=suite_score(head),
        baseline_run_id="run-base",
    )
    assert "Not run on head" in body
    assert "`withdrawn-task`" in body


def test_stale_cassette_rate_counts_only_recording_misses() -> None:
    stale = run(
        tasks=(
            TaskResult(
                task_slug="t",
                trials=(
                    trial("t", 0, Outcome.FAIL, 'model returned 424: {"type":"cassette_miss"}'),
                    trial("t", 1, Outcome.FAIL, "total_cents is 3999, expected 4999"),
                    trial("t", 2, Outcome.PASS),
                ),
            ),
        )
    )
    assert stale_cassette_rate(stale) == pytest.approx(1 / 3)
    assert stale_cassette_rate(run()) == 0.0


# -- sensitivity -------------------------------------------------------------


def test_the_comment_says_what_the_run_could_not_have_detected() -> None:
    """A verdict without its sensitivity is half a sentence."""
    baseline = {f"t{i}": 1.0 for i in range(7)}
    head = {**baseline, "t5": 0.6, "t6": 0.6}
    note = "\n".join(sensitivity_note(baseline, head, target=0.03))

    assert "Sensitivity" in note
    assert "gone unnoticed" in note


def test_an_insensitive_run_says_how_many_tasks_it_would_take() -> None:
    """The remedy is tasks to write, not a threshold to loosen."""
    baseline = {f"t{i}": 1.0 for i in range(7)}
    head = {**baseline, "t5": 0.6, "t6": 0.6}
    note = "\n".join(sensitivity_note(baseline, head, target=0.03))

    assert "comparable tasks" in note
    assert "this comparison has 7" in note


def test_a_sensitive_enough_run_does_not_nag_about_task_count() -> None:
    baseline = {f"t{i}": 1.0 for i in range(40)}
    head = {**baseline, "t5": 0.98}
    note = "\n".join(sensitivity_note(baseline, head, target=0.5))

    assert "Sensitivity" in note
    assert "comparable tasks" not in note


def test_too_few_tasks_says_so_rather_than_quoting_a_number() -> None:
    note = "\n".join(sensitivity_note({"a": 1.0}, {"a": 0.5}, target=0.03))
    assert "too few to estimate" in note
    assert "gone unnoticed" not in note


# -- k is not silently lowered -----------------------------------------------


def _task_with(slug: str, *, passes: int, harness_errors: int) -> TaskResult:
    trials = [
        TrialResult(
            run_id="r",
            task_slug=slug,
            trial_index=i,
            seed=0,
            outcome=Outcome.PASS if i < passes else Outcome.FAIL,
        )
        for i in range(passes)
    ]
    trials += [
        TrialResult(
            run_id="r",
            task_slug=slug,
            trial_index=100 + i,
            seed=0,
            outcome=Outcome.HARNESS_ERROR,
            error_cause=ErrorCause.INTERNAL,
        )
        for i in range(harness_errors)
    ]
    return TaskResult(task_slug=slug, trials=tuple(trials))


def _run_with(*tasks: TaskResult, k: int = 3) -> RunResult:
    return RunResult(
        run_id="r",
        suite_slug="s",
        suite_version=1,
        status=RunStatus.COMPLETE,
        k=k,
        n_requested=5,
        tasks=tasks,
    )


def test_a_task_with_fewer_than_k_trials_is_excluded_not_clamped() -> None:
    """Clamping reported pass^2 as pass^3 = 1.000 and fed it to the aggregate."""
    run = _run_with(_task_with("starved", passes=2, harness_errors=3), k=3)
    assert per_task_scores(run) == {}


def test_a_task_with_exactly_k_trials_is_scored() -> None:
    run = _run_with(_task_with("thin", passes=3, harness_errors=2), k=3)
    assert per_task_scores(run) == {"thin": pytest.approx(1.0)}


def test_a_starved_task_does_not_reach_the_suite_aggregate() -> None:
    """It would have entered as a perfect score and lifted the suite number."""
    run = _run_with(
        _task_with("healthy", passes=3, harness_errors=0),
        _task_with("starved", passes=2, harness_errors=3),
        k=3,
    )
    assert set(per_task_scores(run)) == {"healthy"}


def test_a_zero_tolerance_does_not_crash_the_gate() -> None:
    """`gate.tolerance: "0"` is a valid config — "any drop is a regression".

    `required_tasks` refuses a non-positive effect, and an escaping ValueError
    would leave typer uncaught and exit 1 — which under the exit-code contract
    means `gate` returned FAIL. CI would read a harness crash as a regression.
    """
    baseline = {f"t{i}": 1.0 for i in range(7)}
    head = {**baseline, "t5": 0.6, "t6": 0.4}
    note = "\n".join(sensitivity_note(baseline, head, target=0.0))
    assert "Sensitivity" in note
    assert "comparable tasks" not in note


# -- both suite means must share a denominator --------------------------------


def test_the_two_suite_means_are_averaged_over_the_tasks_they_share() -> None:
    """The verdict is their difference, so a shared denominator is not optional."""
    baseline = {"a": 1.0, "b": 1.0, "dropped": 0.0}
    head = {"a": 1.0, "b": 1.0}
    base_mean, head_mean = paired_suite_scores(baseline, head)
    assert base_mean == pytest.approx(1.0)
    assert head_mean == pytest.approx(1.0)


def test_a_task_lost_on_head_cannot_manufacture_a_drop() -> None:
    """The regression this fixes: identical agent behaviour reported as a drop.

    Three harness errors on one passing task of twelve drop it from head's
    scores. Averaged over different denominators that moves the suite mean
    enough to trip a 0.02 tolerance while the agent behaved identically.
    """
    baseline = {f"pass{i}": 1.0 for i in range(6)} | {f"fail{i}": 0.0 for i in range(6)}
    head = {slug: value for slug, value in baseline.items() if slug != "pass0"}

    naive_drop = (sum(baseline.values()) / len(baseline)) - (sum(head.values()) / len(head))
    assert naive_drop > 0.02, "the unpaired comparison really does invent a drop"

    base_mean, head_mean = paired_suite_scores(baseline, head)
    assert base_mean is not None
    assert base_mean - head_mean == pytest.approx(0.0)


def test_no_shared_tasks_reports_no_baseline() -> None:
    base_mean, head_mean = paired_suite_scores({"gone": 1.0}, {"new": 0.5})
    assert base_mean is None
    assert head_mean == pytest.approx(0.5)
