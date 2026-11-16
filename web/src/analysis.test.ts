import { describe, expect, it } from "vitest";
import { compareRuns, passKCurve, summarize } from "./analysis";
import { RunResultSchema, type Outcome, type RunResult } from "./schema";

function task(slug: string, outcomes: Outcome[], cause?: "model_provider" | "grader") {
  return {
    task_slug: slug,
    trials: outcomes.map((outcome, i) => ({
      task_slug: slug,
      trial_index: i,
      outcome,
      error_cause: outcome === "harness_error" ? (cause ?? "internal") : null,
    })),
  };
}

function run(tasks: ReturnType<typeof task>[], extra: Partial<RunResult> = {}): RunResult {
  return RunResultSchema.parse({
    run_id: "r",
    suite_slug: "s",
    suite_version: 1,
    status: "complete",
    k: 3,
    n_requested: 5,
    tasks,
    ...extra,
  });
}

const P: Outcome = "pass";
const F: Outcome = "fail";
const H: Outcome = "harness_error";

describe("summarize", () => {
  it("excludes harness errors from the denominator", () => {
    const summary = summarize(run([task("a", [P, P, P, P, H])]));
    const a = summary.tasks[0]!;
    expect(a.n).toBe(4);
    expect(a.c).toBe(4);
    expect(a.harnessErrors).toBe(1);
    expect(a.passHatK).toBe(1);
  });

  it("matches the README example: 4 of 5 passing gives pass^3 = 0.4", () => {
    const summary = summarize(run([task("a", [P, P, P, P, F])]));
    expect(summary.tasks[0]!.passHatK).toBeCloseTo(0.4, 12);
  });

  it("gives each task's pass rate a Wilson interval", () => {
    const [lo, hi] = summarize(run([task("a", [P, P, P, P, F])])).tasks[0]!.passRateInterval!;
    expect(lo).toBeCloseTo(0.3755, 3);
    expect(hi).toBeCloseTo(0.9638, 3);
  });

  it("has no pass-rate interval when there are no gradeable trials", () => {
    expect(summarize(run([task("a", [H, H])])).tasks[0]!.passRateInterval).toBeNull();
  });

  it("lowers k when fewer trials were gradeable, and says so", () => {
    const a = summarize(run([task("a", [P, P, H, H, H])])).tasks[0]!;
    expect(a.kUsed).toBe(2);
    expect(a.passHatK).toBe(1);
  });

  it("a task with no gradeable trials is not scored as zero", () => {
    const summary = summarize(run([task("a", [H, H]), task("b", [P, P, P])]));
    expect(summary.tasks[0]!.passHatK).toBeNull();
    expect(summary.suitePassHatK).toBe(1);
  });

  it("suite score is the unweighted mean over tasks", () => {
    // task a: 5 trials, 5 pass -> 1; task b: 5 trials, 0 pass -> 0
    const summary = summarize(run([task("a", [P, P, P, P, P]), task("b", [F, F, F, F, F])]));
    expect(summary.suitePassHatK).toBe(0.5);
    expect(summary.suiteInterval).not.toBeNull();
  });

  it("gives no interval for a single task", () => {
    expect(summarize(run([task("a", [P, F, P, F, P])])).suiteInterval).toBeNull();
  });

  it("breaks harness errors down by cause, most frequent first", () => {
    const summary = summarize(
      run([task("a", [H, H, P], "model_provider"), task("b", [H, P, P], "grader")]),
    );
    expect(summary.harnessErrorsByCause).toEqual([
      ["model_provider", 2],
      ["grader", 1],
    ]);
    expect(summary.harnessErrorRate).toBeCloseTo(3 / 6, 12);
  });

  it("leaves excluded tasks out of the score", () => {
    const summary = summarize(
      run([task("a", [P, P, P]), task("probe", [F, F, F])], { excluded_task_slugs: ["probe"] }),
    );
    expect(summary.tasks.map((t) => t.slug)).toEqual(["a"]);
  });
});

describe("passKCurve", () => {
  it("pass^k falls and pass@k rises as k grows", () => {
    const curve = passKCurve(run([task("a", [P, P, P, F, F])]), 5);
    expect(curve.map((p) => p.k)).toEqual([1, 2, 3, 4, 5]);
    expect(curve[0]!.passHatK).toBeCloseTo(0.6, 12);
    for (let i = 1; i < curve.length; i += 1) {
      expect(curve[i]!.passHatK).toBeLessThanOrEqual(curve[i - 1]!.passHatK);
      expect(curve[i]!.passAtK).toBeGreaterThanOrEqual(curve[i - 1]!.passAtK);
    }
  });

  it("stops where no task has enough trials, instead of clamping", () => {
    const curve = passKCurve(run([task("a", [P, P, P])]), 10);
    expect(curve.map((p) => p.k)).toEqual([1, 2, 3]);
  });
});

describe("compareRuns", () => {
  it("pairs tasks by slug and sorts regressions first", () => {
    const before = summarize(run([task("a", [P, P, P, P, P]), task("b", [F, F, F, F, F])]));
    const after = summarize(run([task("a", [F, F, F, F, F]), task("b", [P, P, P, P, P])]));
    const cmp = compareRuns(before, after);
    expect(cmp.tasks.map((t) => [t.slug, t.delta])).toEqual([
      ["a", -1],
      ["b", 1],
    ]);
    expect(cmp.suiteDelta).toBe(0);
  });

  it("reports tasks present in only one run", () => {
    const cmp = compareRuns(
      summarize(run([task("a", [P, P, P])])),
      summarize(run([task("a", [P, P, P]), task("new", [P, P, P])])),
    );
    expect(cmp.unmatched).toEqual(["new"]);
  });

  it("flags a k mismatch", () => {
    const cmp = compareRuns(
      summarize(run([task("a", [P, P, P])], { k: 2 })),
      summarize(run([task("a", [P, P, P])], { k: 3 })),
    );
    expect(cmp.kMismatch).toBe(true);
  });
});
