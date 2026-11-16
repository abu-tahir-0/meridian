import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import sample from "../../public/sample-run.json";
import App from "../App";
import { parseRun } from "../schema";
import { ErrorBoundary } from "./ErrorBoundary";

vi.mock("./TaskTable", () => ({
  TaskTable: () => {
    throw new Error("the task table exploded");
  },
}));

function loaded(data: unknown, name: string) {
  const parsed = parseRun(JSON.stringify(data));
  if (!parsed.ok) throw new Error(parsed.error);
  return { run: parsed.run, name };
}

describe("ErrorBoundary", () => {
  let explode = true;

  beforeEach(() => {
    explode = true;
  });

  function Flaky() {
    if (explode) throw new Error("the chart exploded");
    return <p>the chart is back</p>;
  }

  it("shows what failed and retries the children", async () => {
    const user = userEvent.setup();
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("This run could not be displayed");
    expect(alert).toHaveTextContent("the chart exploded");

    explode = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("the chart is back")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the dashboard and its file picker mounted when a panel crashes", () => {
    render(<App initial={{ primary: loaded(sample, "sample-run.json") }} />);

    expect(screen.getByRole("alert")).toHaveTextContent("the task table exploded");
    // The header is outside the boundary, so the picker is still there.
    expect(screen.getByRole("button", { name: "Load a run" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load a baseline to compare" })).toBeInTheDocument();
    expect(screen.queryByTestId("run-title")).toBeNull();
  });
});
