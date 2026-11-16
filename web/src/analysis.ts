import type { ErrorCause, RunResult, TaskResult } from "./schema";
import { mean, passAtK, passHatK, tInterval, wilsonInterval, type Interval } from "./stats";

export interface TaskSummary {
  slug: string;
  /** Trials that produced a verdict about the agent (harness errors excluded). */
  n: number;
  /** Trials that passed. */
  c: number;
  harnessErrors: number;
  /** k actually used: lowered to `n` when fewer than k trials were gradeable. */
  kUsed: number;
  passAtK: number | null;
  passHatK: number | null;
  /** Wilson interval for the per-task pass rate `c / n`; null with no gradeable trials. */
  passRateInterval: Interval | null;
}

export interface RunSummary {
  runId: string;
  suiteSlug: string;
  k: number;
  tasks: TaskSummary[];
  /** Unweighted mean of per-task pass^k over tasks that were gradeable. */
  suitePassHatK: number | null;
  /** Student-t interval over per-task pass^k (df = tasks - 1); null with < 2 tasks. */
  suiteInterval: Interval | null;
  totalTrials: number;
  passedTrials: number;
  harnessErrorRate: number;
  harnessErrorsByCause: Array<[ErrorCause | "unknown", number]>;
  costCents: number;
  durationMs: number;
}

export function summarizeTask(task: TaskResult, k: number): TaskSummary {
  const graded = task.trials.filter((t) => t.outcome !== "harness_error");
  const n = graded.length;
  const c = graded.filter((t) => t.outcome === "pass").length;
  const harnessErrors = task.trials.length - n;
  if (n === 0) {
    return {
      slug: task.task_slug,
      n,
      c,
      harnessErrors,
      kUsed: 0,
      passAtK: null,
      passHatK: null,
      passRateInterval: null,
    };
  }
  const kUsed = Math.min(k, n);
  return {
    slug: task.task_slug,
    n,
    c,
    harnessErrors,
    kUsed,
    passAtK: passAtK(n, c, kUsed),
    passHatK: passHatK(n, c, kUsed),
    passRateInterval: wilsonInterval(c, n),
  };
}

export function summarize(run: RunResult): RunSummary {
  const excluded = new Set(run.excluded_task_slugs);
  const tasks = run.tasks.filter((t) => !excluded.has(t.task_slug)).map((t) => summarizeTask(t, run.k));
  const scored = tasks.filter((t) => t.passHatK !== null);
  const values = scored.map((t) => t.passHatK as number);

  let suiteInterval: Interval | null = null;
  if (values.length >= 2) {
    const [lo, hi] = tInterval(values);
    suiteInterval = [Math.max(lo, 0), Math.min(hi, 1)];
  }

  const allTrials = run.tasks.flatMap((t) => t.trials);
  const causes = new Map<ErrorCause | "unknown", number>();
  for (const trial of allTrials) {
    if (trial.outcome === "harness_error") {
      const key = trial.error_cause ?? "unknown";
      causes.set(key, (causes.get(key) ?? 0) + 1);
    }
  }
  const harnessErrors = [...causes.values()].reduce((a, b) => a + b, 0);

  return {
    runId: run.run_id,
    suiteSlug: run.suite_slug,
    k: run.k,
    tasks,
    suitePassHatK: values.length === 0 ? null : mean(values),
    suiteInterval,
    totalTrials: allTrials.length,
    passedTrials: allTrials.filter((t) => t.outcome === "pass").length,
    harnessErrorRate: allTrials.length === 0 ? 0 : harnessErrors / allTrials.length,
    harnessErrorsByCause: [...causes.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
    costCents: run.cost_cents,
    durationMs: run.duration_ms,
  };
}

export interface CurvePoint {
  k: number;
  passHatK: number;
  passAtK: number;
}

/**
 * Suite pass^k and pass@k for k = 1..kMax. Tasks with fewer than k gradeable
 * trials are left out of that point rather than clamped, so the curve never
 * claims more reliability than the trials support.
 */
export function passKCurve(run: RunResult, kMax: number): CurvePoint[] {
  const excluded = new Set(run.excluded_task_slugs);
  const counts = run.tasks
    .filter((t) => !excluded.has(t.task_slug))
    .map((t) => {
      const graded = t.trials.filter((x) => x.outcome !== "harness_error");
      return { n: graded.length, c: graded.filter((x) => x.outcome === "pass").length };
    });
  const points: CurvePoint[] = [];
  for (let k = 1; k <= kMax; k += 1) {
    const eligible = counts.filter((t) => t.n >= k);
    if (eligible.length === 0) break;
    points.push({
      k,
      passHatK: mean(eligible.map((t) => passHatK(t.n, t.c, k))),
      passAtK: mean(eligible.map((t) => passAtK(t.n, t.c, k))),
    });
  }
  return points;
}

export interface TaskDelta {
  slug: string;
  before: number | null;
  after: number | null;
  /** after - before; null when either side was not gradeable. */
  delta: number | null;
}

export interface Comparison {
  suiteBefore: number | null;
  suiteAfter: number | null;
  suiteDelta: number | null;
  tasks: TaskDelta[];
  /** Tasks present in only one of the runs. */
  unmatched: string[];
  /** The two runs used different k, so per-task scores are not strictly comparable. */
  kMismatch: boolean;
}

/** Paired per-task comparison of two runs, matched by task slug. */
export function compareRuns(before: RunSummary, after: RunSummary): Comparison {
  const afterBySlug = new Map(after.tasks.map((t) => [t.slug, t]));
  const beforeSlugs = new Set(before.tasks.map((t) => t.slug));
  const tasks: TaskDelta[] = [];
  for (const task of before.tasks) {
    const other = afterBySlug.get(task.slug);
    if (!other) continue;
    const delta =
      task.passHatK !== null && other.passHatK !== null ? other.passHatK - task.passHatK : null;
    tasks.push({ slug: task.slug, before: task.passHatK, after: other.passHatK, delta });
  }
  tasks.sort((a, b) => (a.delta ?? 0) - (b.delta ?? 0));
  const unmatched = [
    ...before.tasks.filter((t) => !afterBySlug.has(t.slug)).map((t) => t.slug),
    ...after.tasks.filter((t) => !beforeSlugs.has(t.slug)).map((t) => t.slug),
  ];
  const suiteDelta =
    before.suitePassHatK !== null && after.suitePassHatK !== null
      ? after.suitePassHatK - before.suitePassHatK
      : null;
  return {
    suiteBefore: before.suitePassHatK,
    suiteAfter: after.suitePassHatK,
    suiteDelta,
    tasks,
    unmatched,
    kMismatch: before.k !== after.k,
  };
}
