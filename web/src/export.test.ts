import { describe, expect, it } from "vitest";
import type { Comparison, TaskSummary } from "./analysis";
import { comparisonCsv, comparisonMarkdown, taskTableCsv, taskTableMarkdown } from "./export";

const tasks: TaskSummary[] = [
  {
    slug: "happy-path",
    n: 5,
    c: 4,
    harnessErrors: 0,
    kUsed: 3,
    passAtK: 1,
    passHatK: 0.5,
    passRateInterval: [0.38, 0.96],
  },
  {
    slug: 'quoted, "slug"',
    n: 0,
    c: 0,
    harnessErrors: 2,
    kUsed: 1,
    passAtK: null,
    passHatK: null,
    passRateInterval: null,
  },
];

const comparison: Comparison = {
  suiteBefore: 0.5,
  suiteAfter: 0.75,
  suiteDelta: 0.25,
  tasks: [
    { slug: "happy-path", before: 0.5, after: 0.75, delta: 0.25 },
    { slug: "gone", before: 0.5, after: null, delta: null },
  ],
  unmatched: ["gone"],
  kMismatch: true,
};

describe("task table export", () => {
  it("writes a CSV that quotes commas and quotes", () => {
    const csv = taskTableCsv(tasks, 3);
    const lines = csv.trimEnd().split("\n");
    expect(lines[0]).toBe("task,passed,graded,pass@3,pass^3,harness_errors,notes");
    expect(lines[1]).toBe("happy-path,4,5,1.00,0.50,0,");
    expect(lines[2]).toBe('"quoted, ""slug""",0,0,,,2,no gradeable trials 2 harness error(s)');
    expect(csv.endsWith("\n")).toBe(true);
  });

  it("writes a Markdown table that escapes pipes and keeps ungradeable scores visible", () => {
    const md = taskTableMarkdown(tasks, 3);
    expect(md).toContain("| Task | Passed | Graded | pass@3 | pass^3 | Notes |");
    expect(md).toContain('| quoted, "slug" | 0 | 0 | — | — |');
  });

  it("escapes a pipe in a task slug so the table keeps its columns", () => {
    const md = taskTableMarkdown([{ ...tasks[0]!, slug: "a|b" }], 3);
    expect(md).toContain("| a\\|b | 4 | 5 |");
  });
});

describe("comparison export", () => {
  it("writes the comparison table as CSV, leaving a missing side empty", () => {
    const csv = comparisonCsv(comparison);
    expect(csv).toBe(
      "task,before,after,delta\n" +
        "happy-path,0.50,0.75,0.25\n" +
        "gone,0.50,,\n",
    );
  });

  it("says when k differs and which tasks were only in one run", () => {
    const md = comparisonMarkdown(comparison);
    expect(md).toContain("| Task | Before | After | Change |");
    expect(md).toContain("| happy-path | 0.50 | 0.75 | +0.25 |");
    expect(md).toContain("different k");
    expect(md).toContain("Only in one run: gone");
  });
});
