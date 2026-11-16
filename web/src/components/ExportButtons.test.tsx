import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Comparison, TaskSummary } from "../analysis";
import { Compare } from "./Compare";
import { TaskTable } from "./TaskTable";

const tasks: TaskSummary[] = [
  { slug: "happy-path", n: 5, c: 4, harnessErrors: 0, kUsed: 3, passAtK: 1, passHatK: 0.5 },
];

const comparison: Comparison = {
  suiteBefore: 0.5,
  suiteAfter: 0.75,
  suiteDelta: 0.25,
  tasks: [{ slug: "happy-path", before: 0.5, after: 0.75, delta: 0.25 }],
  unmatched: [],
  kMismatch: false,
};

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:meridian");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("task table exports", () => {
  it("copies the table as Markdown", async () => {
    const user = userEvent.setup();
    render(<TaskTable tasks={tasks} k={3} />);

    await user.click(screen.getByRole("button", { name: "Copy tasks as Markdown" }));

    expect(await navigator.clipboard.readText()).toContain(
      "| Task | Passed | Graded | pass@3 | pass^3 |",
    );
    expect(screen.getByRole("status")).toHaveTextContent("tasks copied as Markdown.");
  });

  it("downloads the CSV through a blob", async () => {
    const user = userEvent.setup();
    render(<TaskTable tasks={tasks} k={3} />);

    await user.click(screen.getByRole("button", { name: "Download tasks CSV" }));

    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    const blob = (URL.createObjectURL as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0]![0] as Blob;
    expect(await blob.text()).toContain("task,passed,graded,pass@3,pass^3");
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:meridian");
    expect(screen.getByRole("status")).toHaveTextContent("Downloaded meridian-tasks.csv.");
  });

  it("says so when the browser blocks the clipboard", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("denied"));
    render(<TaskTable tasks={tasks} k={3} />);

    await user.click(screen.getByRole("button", { name: "Copy tasks as Markdown" }));

    expect(screen.getByRole("status")).toHaveTextContent("blocked clipboard access");
  });
});

describe("comparison exports", () => {
  it("copies and downloads the comparison", async () => {
    const user = userEvent.setup();
    render(<Compare comparison={comparison} />);
    const section = screen.getByRole("region", { name: "Run comparison" });

    await user.click(within(section).getByRole("button", { name: "Copy comparison as Markdown" }));
    expect(await navigator.clipboard.readText()).toContain(
      "| happy-path | 0.50 | 0.75 | +0.25 |",
    );

    await user.click(within(section).getByRole("button", { name: "Download comparison CSV" }));
    const blob = (URL.createObjectURL as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0]![0] as Blob;
    expect(await blob.text()).toBe("task,before,after,delta\nhappy-path,0.50,0.75,0.25\n");
    expect(screen.getByRole("status")).toHaveTextContent("Downloaded meridian-comparison.csv.");
  });
});
