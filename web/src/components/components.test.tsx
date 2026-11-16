import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Comparison } from "../analysis";
import type { RunResult } from "../schema";
import { Compare } from "./Compare";
import { PassKCurve } from "./PassKCurve";
import { TrialHeatmap } from "./TrialHeatmap";

function trial(task: string, index: number, outcome: "pass" | "fail" | "harness_error") {
  return {
    task_slug: task,
    trial_index: index,
    outcome,
    detail: "",
    attempts: 1,
    efficiency: { turns: 1, tool_calls: 0, input_tokens: 0, output_tokens: 0, duration_ms: 1 },
    error_cause: null,
  };
}

const run: RunResult = {
  run_id: "r",
  suite_slug: "s",
  suite_version: 1,
  status: "complete",
  k: 3,
  n_requested: 2,
  tasks: [
    {
      task_slug: "alpha",
      // Deliberately out of order: the heatmap sorts by trial index.
      trials: [trial("alpha", 2, "fail"), trial("alpha", 0, "pass"), trial("alpha", 1, "harness_error")],
    },
    { task_slug: "excluded", trials: [trial("excluded", 0, "pass")] },
  ],
  excluded_task_slugs: ["excluded"],
  detail: "",
  duration_ms: 0,
  manifest_hash: null,
  cost_cents: 0,
};

const comparison: Comparison = {
  suiteBefore: 0.4,
  suiteAfter: 0.6,
  suiteDelta: 0.2,
  tasks: [{ slug: "alpha", before: 0.4, after: 0.6, delta: 0.2 }],
  unmatched: ["beta"],
  kMismatch: true,
};

describe("PassKCurve", () => {
  it("asks for more trials when there are fewer than two points", () => {
    render(<PassKCurve points={[{ k: 1, passAtK: 1, passHatK: 0.5 }]} />);
    expect(screen.getByText(/Needs at least 2 gradeable trials/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("draws the curve when there are enough points", () => {
    render(
      <PassKCurve
        points={[
          { k: 1, passAtK: 0.8, passHatK: 0.8 },
          { k: 2, passAtK: 0.9, passHatK: 0.6 },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: /pass at k and pass hat k/ })).toBeInTheDocument();
  });
});

describe("Compare", () => {
  it("warns when the two runs used different k", () => {
    render(<Compare comparison={comparison} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/different k/);
  });

  it("shows the suite delta and the tasks found in only one run", () => {
    render(<Compare comparison={comparison} />);
    expect(screen.getByTestId("suite-delta")).toHaveTextContent("(+0.20)");
    expect(screen.getByText("Only in one run: beta")).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /alpha/ });
    expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "0.40",
      "0.60",
      "+0.20",
    ]);
  });

  it("does not warn when k matches", () => {
    render(<Compare comparison={{ ...comparison, kMismatch: false }} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("TrialHeatmap", () => {
  it("orders trials by index and labels each outcome", () => {
    render(<TrialHeatmap run={run} />);
    const row = screen.getByText("alpha").closest(".heat-row");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getAllByRole("img").map((cell) => cell.getAttribute("aria-label"))).toEqual([
      "alpha trial 0: pass",
      "alpha trial 1: harness error",
      "alpha trial 2: fail",
    ]);
  });

  it("leaves excluded tasks out of the grid", () => {
    render(<TrialHeatmap run={run} />);
    expect(screen.queryByText("excluded")).not.toBeInTheDocument();
  });
});
