"""The CLI table.

pass@k and pass^k sit side by side deliberately. Seeing `pass@3 = 1.00` next to
`pass^3 = 0.40` on the same row is the fastest way to understand why a mean score
is not good enough — and it is the line the demo is built around.
"""

from __future__ import annotations

from meridian.models.run import RunResult, TaskResult
from meridian.stats.bootstrap import bootstrap_ci
from meridian.stats.intervals import t_interval, wilson_interval
from meridian.stats.passk import pass_at_k, pass_hat_k

BAR = "─"


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def task_row(result: TaskResult, k: int) -> str:
    n, c = result.n, result.c
    if n == 0:
        return (
            f"{result.task_slug:<24} {'—':>5} {'—':>5} {'—':>8} {'—':>8} {'—':>16}"
            "   no gradeable trials"
        )
    effective_k = min(k, n)
    at_k = pass_at_k(n, c, effective_k)
    hat_k = pass_hat_k(n, c, effective_k)
    lo, hi = wilson_interval(c, n)
    note = "" if effective_k == k else f"  (k lowered to {effective_k}: only {n} trials)"
    errors = f"  {result.harness_errors} harness error(s)" if result.harness_errors else ""
    interval = f"[{_fmt(lo)}, {_fmt(hi)}]"
    return (
        f"{result.task_slug:<24} {c:>5} {n:>5} {_fmt(at_k):>8} {_fmt(hat_k):>8}"
        f" {interval:>16}{note}{errors}"
    )


def render(run: RunResult) -> str:
    """The per-task table plus the suite aggregate."""
    lines: list[str] = []
    lines.append(f"run {run.run_id} — suite {run.suite_slug} v{run.suite_version}")
    lines.append(f"status: {run.status}  n={run.n_requested}  k={run.k}")
    if run.detail:
        lines.append(f"note: {run.detail}")
    lines.append("")
    lines.append(
        f"{'task':<24} {'pass':>5} {'n':>5} {'pass@' + str(run.k):>8}"
        f" {'pass^' + str(run.k):>8} {'95% CI':>16}"
    )
    lines.append(BAR * 72)
    for task in run.tasks:
        lines.append(task_row(task, run.k))
    lines.append(BAR * 72)

    scored = [t for t in run.tasks if t.n > 0]
    if scored:
        suite_hat = sum(pass_hat_k(t.n, t.c, min(run.k, t.n)) for t in scored) / len(scored)
        lines.append(f"{'suite pass^' + str(run.k):<24} {_fmt(suite_hat):>28}")
        if len(scored) > 1:
            # Resampled over tasks, not trials — trials within a task are
            # correlated and a trial-level interval would be far too narrow.
            lo, hi = bootstrap_ci([(t.n, t.c, run.k) for t in scored], pass_hat_k, iterations=4000)
            lines.append(f"{'95% CI (over tasks)':<24} {f'[{_fmt(lo)}, {_fmt(hi)}]':>28}")
            # With few tasks the bootstrap undercovers; a t interval on tasks - 1
            # degrees of freedom is wider and more honest at small m.
            per_task = [pass_hat_k(t.n, t.c, min(run.k, t.n)) for t in scored]
            t_lo, t_hi = t_interval(per_task)
            lines.append(
                f"{'95% CI (t, df=tasks-1)':<24} "
                f"{f'[{_fmt(max(t_lo, 0.0))}, {_fmt(min(t_hi, 1.0))}]':>28}"
            )
    if run.excluded_task_slugs:
        lines.append(f"excluded from the aggregate: {', '.join(run.excluded_task_slugs)}")
    lines.append(f"harness error rate: {run.harness_error_summary}")
    lines.append(f"cost: {run.cost_cents}c   wall clock: {run.duration_ms / 1000:.1f}s")
    return "\n".join(lines)


def failures(run: RunResult, limit: int = 12) -> str:
    """Why the failing trials failed, in the words the grader used."""
    lines: list[str] = []
    for task in run.tasks:
        for trial in task.trials:
            if trial.passed or not trial.detail:
                continue
            lines.append(f"  {task.task_slug}[{trial.trial_index}] {trial.outcome}: {trial.detail}")
            if len(lines) >= limit:
                lines.append(f"  … and more; {limit} shown")
                return "\n".join(lines)
    return "\n".join(lines)
