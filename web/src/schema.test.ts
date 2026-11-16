import { describe, expect, it } from "vitest";
import sample from "../public/sample-run.json";
import { parseRun } from "./schema";

describe("parseRun", () => {
  it("accepts a real Meridian result (the bundled sample)", () => {
    const parsed = parseRun(JSON.stringify(sample));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.run.suite_slug).toBe("checkout-agent");
      expect(parsed.run.tasks).toHaveLength(7);
    }
  });

  it("accepts older results that predate error_cause", () => {
    const old = {
      run_id: "r",
      suite_slug: "s",
      suite_version: 1,
      status: "complete",
      k: 3,
      n_requested: 1,
      tasks: [{ task_slug: "a", trials: [{ task_slug: "a", trial_index: 0, outcome: "fail" }] }],
    };
    expect(parseRun(JSON.stringify(old)).ok).toBe(true);
  });

  it("accepts newer results with fields this dashboard does not know", () => {
    const future = {
      run_id: "r",
      suite_slug: "s",
      suite_version: 1,
      status: "complete",
      k: 3,
      n_requested: 1,
      added_by_a_later_release: { nested: true },
      tasks: [
        {
          task_slug: "a",
          task_level_extra: "ignored",
          trials: [
            {
              task_slug: "a",
              trial_index: 0,
              outcome: "pass",
              trial_level_extra: [1, 2, 3],
              efficiency: { turns: 2, cache_hits: 4 },
            },
          ],
        },
      ],
    };
    const parsed = parseRun(JSON.stringify(future));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.run.tasks[0]?.trials[0]?.outcome).toBe("pass");
      expect(parsed.run.tasks[0]?.trials[0]?.efficiency.turns).toBe(2);
    }
  });

  it("rejects text that is not JSON", () => {
    const parsed = parseRun("not json {");
    expect(parsed).toEqual({ ok: false, error: "That file is not valid JSON." });
  });

  it("rejects JSON that is not a run result and says where", () => {
    const parsed = parseRun(JSON.stringify({ run_id: "r", tasks: "nope" }));
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toMatch(/not a Meridian run result/);
  });

  it("rejects an unknown outcome rather than guessing", () => {
    const bad = {
      run_id: "r",
      suite_slug: "s",
      suite_version: 1,
      status: "complete",
      k: 3,
      n_requested: 1,
      tasks: [{ task_slug: "a", trials: [{ task_slug: "a", trial_index: 0, outcome: "maybe" }] }],
    };
    expect(parseRun(JSON.stringify(bad)).ok).toBe(false);
  });
});
