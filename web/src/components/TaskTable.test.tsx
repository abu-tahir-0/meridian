import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TaskSummary } from "../analysis";
import { TaskTable } from "./TaskTable";

const tasks: TaskSummary[] = [
  {
    slug: "alpha",
    n: 5,
    c: 4,
    harnessErrors: 0,
    kUsed: 3,
    passAtK: 1,
    passHatK: 0.4,
    passRateInterval: [0.38, 0.96],
  },
  {
    slug: "ungraded",
    n: 0,
    c: 0,
    harnessErrors: 2,
    kUsed: 0,
    passAtK: null,
    passHatK: null,
    passRateInterval: null,
  },
];

describe("TaskTable", () => {
  it("shows the Wilson interval beside the raw pass count", () => {
    render(<TaskTable tasks={tasks} k={3} />);
    const cells = within(screen.getByRole("row", { name: /alpha/ })).getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toEqual(["4", "5", "[0.38, 0.96]", "1.00", "0.40", ""]);
  });

  it("does not invent an interval for a task with no gradeable trials", () => {
    render(<TaskTable tasks={tasks} k={3} />);
    const cells = within(screen.getByRole("row", { name: /ungraded/ })).getAllByRole("cell");
    expect(cells[2]?.textContent).toBe("—");
  });
});
