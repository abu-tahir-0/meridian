import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import sample from "../public/sample-run.json";
import baseline from "../public/sample-baseline.json";
import App from "./App";
import { parseRun } from "./schema";

function loaded(data: unknown, name: string) {
  const parsed = parseRun(JSON.stringify(data));
  if (!parsed.ok) throw new Error(parsed.error);
  return { run: parsed.run, name };
}

describe("App", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  it("applies and remembers an explicit theme", async () => {
    const user = userEvent.setup();
    const view = render(<App />);
    await user.selectOptions(screen.getByRole("combobox", { name: "Theme" }), "dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("meridian.dashboard.theme")).toBe("dark");

    view.unmount();
    render(<App />);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.getByRole("combobox", { name: "Theme" })).toHaveValue("dark");
  });

  it("falls back to the system theme for an unknown stored choice", () => {
    localStorage.setItem("meridian.dashboard.theme", "sepia");
    render(<App />);
    expect(screen.getByRole("combobox", { name: "Theme" })).toHaveValue("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("starts empty and invites you to load a run", () => {
    render(<App />);
    expect(screen.getByText("No run loaded yet.")).toBeInTheDocument();
  });

  it("shows the headline numbers for a loaded run", () => {
    render(<App initial={{ primary: loaded(sample, "sample-run.json") }} />);
    // Matches the Python CLI table for this run: suite pass^3 0.00 (exactly 0.0048).
    expect(screen.getByTestId("suite-score")).toHaveTextContent(/^0\.00$/);
    expect(screen.getByText(/95% CI \[0\.00, 0\.02\] \(t, df=6\)/)).toBeInTheDocument();
    expect(screen.getByTestId("harness-rate")).toHaveTextContent("4.3%");
    expect(screen.getByText(/model_provider: 3/)).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("lists every scored task and leaves the probes out", () => {
    render(<App initial={{ primary: loaded(sample, "sample-run.json") }} />);
    const table = screen.getByRole("region", { name: "Per-task results" });
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 7);
    expect(within(table).queryByText("contamination-probe")).toBeNull();
  });

  it("draws one accessible cell per trial", () => {
    render(<App initial={{ primary: loaded(sample, "sample-run.json") }} />);
    const grid = screen.getByRole("region", { name: "Trial outcomes" });
    expect(within(grid).getAllByRole("img")).toHaveLength(70);
    expect(within(grid).getAllByLabelText(/harness error \(model_provider\)/)).toHaveLength(3);
  });

  it("compares against a baseline and flags regressions in red", () => {
    render(
      <App
        initial={{
          primary: loaded(sample, "sample-run.json"),
          baseline: loaded(baseline, "sample-baseline.json"),
        }}
      />,
    );
    const compare = screen.getByRole("region", { name: "Run comparison" });
    expect(within(compare).getByTestId("suite-delta")).toHaveClass("bad");
  });

  it("explains a file that is not a run result instead of crashing", async () => {
    const user = userEvent.setup();
    render(<App />);
    const input = screen.getByLabelText("Load a run");
    const file = new File(["{\"hello\": 1}"], "x.json", { type: "application/json" });
    await user.upload(input, file);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a Meridian run result/);
  });

  it("highlights a drop zone while a file is over it, not when it crosses a child", () => {
    render(<App />);
    const drop = screen.getByRole("button", { name: "Load a run" }).closest(".drop");
    expect(drop).not.toBeNull();
    const hint = screen.getAllByText(/or drop a result\.json here/)[0]!;

    fireEvent.dragEnter(drop!);
    fireEvent.dragEnter(hint);
    expect(drop).toHaveClass("dragging");

    fireEvent.dragLeave(hint);
    expect(drop).toHaveClass("dragging");

    fireEvent.dragLeave(drop!);
    expect(drop).not.toHaveClass("dragging");
  });

  it("loads a real run file from disk", async () => {
    const user = userEvent.setup();
    render(<App />);
    const file = new File([JSON.stringify(sample)], "result.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("Load a run"), file);
    expect(await screen.findByTestId("run-title")).toHaveTextContent("result.json");
  });
});
