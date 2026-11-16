import { useMemo, useState } from "react";
import { compareRuns, passKCurve, summarize } from "./analysis";
import { Compare } from "./components/Compare";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { FileDrop } from "./components/FileDrop";
import { PassKCurve } from "./components/PassKCurve";
import { SummaryCards } from "./components/SummaryCards";
import { TaskTable } from "./components/TaskTable";
import { TrialHeatmap } from "./components/TrialHeatmap";
import { parseRun, type RunResult } from "./schema";

interface Loaded {
  run: RunResult;
  name: string;
}

export interface AppProps {
  /** Optional preloaded runs (used by tests and the bundled samples). */
  initial?: { primary?: Loaded; baseline?: Loaded };
}

export default function App({ initial }: AppProps) {
  const [primary, setPrimary] = useState<Loaded | undefined>(initial?.primary);
  const [baseline, setBaseline] = useState<Loaded | undefined>(initial?.baseline);
  const [sampleError, setSampleError] = useState<string | null>(null);

  const summary = useMemo(() => (primary ? summarize(primary.run) : null), [primary]);
  const curve = useMemo(
    () => (primary ? passKCurve(primary.run, Math.max(primary.run.n_requested, primary.run.k)) : []),
    [primary],
  );
  const comparison = useMemo(
    () => (primary && baseline ? compareRuns(summarize(baseline.run), summarize(primary.run)) : null),
    [primary, baseline],
  );

  async function loadSample(file: string, set: (l: Loaded) => void, name: string) {
    try {
      const response = await fetch(`./${file}`);
      const parsed = parseRun(await response.text());
      if (parsed.ok) {
        set({ run: parsed.run, name });
        setSampleError(null);
      } else {
        setSampleError(parsed.error);
      }
    } catch {
      setSampleError("Could not load the bundled sample.");
    }
  }

  return (
    <main>
      <header>
        <h1>Meridian dashboard</h1>
        <p className="muted">
          Load a <code>result.json</code> from <code>meridian run --out</code>. Everything runs in
          your browser; nothing is uploaded.
        </p>
        <div className="toolbar">
          <FileDrop label="Load a run" onLoad={(run, name) => setPrimary({ run, name })} />
          <FileDrop label="Load a baseline to compare" onLoad={(run, name) => setBaseline({ run, name })} />
          <button
            type="button"
            onClick={() => {
              void loadSample("sample-run.json", setPrimary, "sample-run.json");
              void loadSample("sample-baseline.json", setBaseline, "sample-baseline.json");
            }}
          >
            Try the sample
          </button>
        </div>
        {sampleError && (
          <p role="alert" className="bad">
            {sampleError}
          </p>
        )}
      </header>

      {primary && summary ? (
        <ErrorBoundary
          key={`${primary.name}:${primary.run.run_id}:${baseline?.run.run_id ?? ""}`}
        >
          <p className="muted" data-testid="run-title">
            {primary.name} — {summary.suiteSlug} · run {summary.runId} · n={primary.run.n_requested}, k=
            {summary.k}
          </p>
          {primary.run.detail && <p className="note">{primary.run.detail}</p>}
          <SummaryCards summary={summary} />
          <TaskTable tasks={summary.tasks} k={summary.k} />
          <TrialHeatmap run={primary.run} />
          <PassKCurve points={curve} />
          {comparison && <Compare comparison={comparison} />}
        </ErrorBoundary>
      ) : (
        <p className="muted">No run loaded yet.</p>
      )}
    </main>
  );
}
