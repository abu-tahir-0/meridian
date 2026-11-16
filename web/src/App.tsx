import { useEffect, useMemo, useRef, useState } from "react";
import { compareRuns, passKCurve, summarize } from "./analysis";
import { Compare } from "./components/Compare";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { FileDrop } from "./components/FileDrop";
import { PassKCurve } from "./components/PassKCurve";
import { SummaryCards } from "./components/SummaryCards";
import { TaskTable } from "./components/TaskTable";
import { TrialHeatmap } from "./components/TrialHeatmap";
import { resolveRunUrl, runNameFor } from "./link";
import { parseRun, type RunResult } from "./schema";
import { applyTheme, isTheme, readTheme, saveTheme, type Theme } from "./theme";

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
  const [loadError, setLoadError] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const linkLoaded = useRef(false);

  useEffect(() => {
    applyTheme(theme);
    saveTheme(theme);
  }, [theme]);

  const summary = useMemo(() => (primary ? summarize(primary.run) : null), [primary]);
  const curve = useMemo(
    () => (primary ? passKCurve(primary.run, Math.max(primary.run.n_requested, primary.run.k)) : []),
    [primary],
  );
  const comparison = useMemo(
    () => (primary && baseline ? compareRuns(summarize(baseline.run), summarize(primary.run)) : null),
    [primary, baseline],
  );

  useEffect(() => {
    if (initial || linkLoaded.current) return;
    linkLoaded.current = true;
    const params = new URLSearchParams(window.location.search);
    const links: Array<{ slot: string; raw: string | null; set: (l: Loaded) => void }> = [
      { slot: "run", raw: params.get("run"), set: setPrimary },
      { slot: "baseline", raw: params.get("baseline"), set: setBaseline },
    ];
    for (const { slot, raw, set } of links) {
      if (!raw) continue;
      const url = resolveRunUrl(raw, window.location.href);
      if (!url) {
        setLoadError(`Ignoring the ${slot} link: only http and https URLs can be loaded.`);
        continue;
      }
      void loadLinkedRun(slot, url, set);
    }
  }, [initial]);

  async function loadSample(file: string, set: (l: Loaded) => void, name: string) {
    try {
      const response = await fetch(`./${file}`);
      const parsed = parseRun(await response.text());
      if (parsed.ok) {
        set({ run: parsed.run, name });
        setLoadError(null);
      } else {
        setLoadError(parsed.error);
      }
    } catch {
      setLoadError("Could not load the bundled sample.");
    }
  }

  async function loadLinkedRun(slot: string, url: URL, set: (l: Loaded) => void) {
    try {
      const response = await fetch(url.href);
      if (!response.ok) {
        setLoadError(`Could not load the ${slot} from ${url.href}: HTTP ${response.status}.`);
        return;
      }
      const parsed = parseRun(await response.text());
      if (!parsed.ok) {
        setLoadError(`The ${slot} at ${url.href} is not a Meridian run result: ${parsed.error}`);
        return;
      }
      set({ run: parsed.run, name: runNameFor(url) });
      setLoadError(null);
    } catch {
      setLoadError(
        `Could not reach ${url.href} for the ${slot}. Check the link and that the server allows cross-origin (CORS) requests.`,
      );
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
          <label className="theme-picker">
            Theme
            <select
              value={theme}
              onChange={(event) => {
                if (isTheme(event.target.value)) setTheme(event.target.value);
              }}
            >
              <option value="system">System</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
        </div>
        {loadError && (
          <p role="alert" className="bad">
            {loadError}
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
