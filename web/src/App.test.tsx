import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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


  it("loads a run from a ?run= link and names it after the file", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(sample),
    }));
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/?run=https%3A%2F%2Fexample.test%2Fresult.json");
    render(<App />);
    expect(await screen.findByTestId("run-title")).toHaveTextContent("result.json");
    expect(fetchMock).toHaveBeenCalledWith("https://example.test/result.json");
  });

  it("loads a baseline from a &baseline= link", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, text: async () => JSON.stringify(sample) })),
    );
    window.history.replaceState(
      {},
      "",
      "/?run=https%3A%2F%2Fexample.test%2Fresult.json&baseline=https%3A%2F%2Fexample.test%2Fbefore.json",
    );
    render(<App />);
    expect(await screen.findByRole("region", { name: "Run comparison" })).toBeInTheDocument();
  });

  it("refuses links that are not http or https", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/?run=javascript%3Aalert(1)");
    render(<App />);
    expect(screen.getByRole("alert")).toHaveTextContent(/only http and https/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("explains a linked run that cannot be fetched", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    window.history.replaceState({}, "", "/?run=https%3A%2F%2Fexample.test%2Frun.json");
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/allows cross-origin \(CORS\)/i);
  });

  it("explains a linked file that is not a run result", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, text: async () => "{\"hello\": 1}" })),
    );
    window.history.replaceState({}, "", "/?run=https%3A%2F%2Fexample.test%2Frun.json");
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a Meridian run result/);
  });

  it("loads a real run file from disk", async () => {
    const user = userEvent.setup();
    render(<App />);
    const file = new File([JSON.stringify(sample)], "result.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("Load a run"), file);
    expect(await screen.findByTestId("run-title")).toHaveTextContent("result.json");
  });
});
