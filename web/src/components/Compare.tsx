import type { Comparison } from "../analysis";
import { comparisonCsv, comparisonMarkdown } from "../export";
import { fmt, fmtSigned } from "../format";
import { ExportButtons } from "./ExportButtons";

export function Compare({ comparison }: { comparison: Comparison }) {
  return (
    <section aria-label="Run comparison">
      <h2>Comparison</h2>
      <ExportButtons
        label="comparison"
        filename="meridian-comparison.csv"
        markdown={comparisonMarkdown(comparison)}
        csv={comparisonCsv(comparison)}
      />
      <p>
        Suite pass^k: <strong>{fmt(comparison.suiteBefore)}</strong> →{" "}
        <strong>{fmt(comparison.suiteAfter)}</strong>{" "}
        <span
          data-testid="suite-delta"
          className={
            comparison.suiteDelta === null
              ? "muted"
              : comparison.suiteDelta < 0
                ? "bad"
                : comparison.suiteDelta > 0
                  ? "good"
                  : "muted"
          }
        >
          ({fmtSigned(comparison.suiteDelta)})
        </span>
      </p>
      {comparison.kMismatch && (
        <p role="alert" className="warn">
          The runs used different k, so per-task scores are not strictly comparable.
        </p>
      )}
      <p className="muted">
        With few tasks and trials a difference this size may be noise; Meridian&apos;s gate uses a
        paired test and reports the smallest effect it can detect.
      </p>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Task</th>
              <th scope="col" className="num">
                Before
              </th>
              <th scope="col" className="num">
                After
              </th>
              <th scope="col" className="num">
                Change
              </th>
            </tr>
          </thead>
          <tbody>
            {comparison.tasks.map((t) => (
              <tr key={t.slug}>
                <th scope="row">{t.slug}</th>
                <td className="num">{fmt(t.before)}</td>
                <td className="num">{fmt(t.after)}</td>
                <td className={`num ${t.delta === null ? "" : t.delta < 0 ? "bad" : t.delta > 0 ? "good" : ""}`}>
                  {fmtSigned(t.delta)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {comparison.unmatched.length > 0 && (
        <p className="muted">Only in one run: {comparison.unmatched.join(", ")}</p>
      )}
    </section>
  );
}
