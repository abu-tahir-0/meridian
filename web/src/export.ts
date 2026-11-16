import type { Comparison, TaskSummary } from "./analysis";
import { fmt, fmtSigned } from "./format";

/** The note shown in the task table's last column. Kept here so the export cannot drift. */
export function taskNote(task: TaskSummary, k: number): string {
  const lowered =
    task.n === 0
      ? "no gradeable trials"
      : task.kUsed < k
        ? `k lowered to ${task.kUsed}: only ${task.n} gradeable`
        : "";
  const errors = task.harnessErrors > 0 ? `${task.harnessErrors} harness error(s)` : "";
  return [lowered, errors].filter(Boolean).join(" ");
}

function csvCell(value: string): string {
  if (/[",\n\r]/.test(value)) {
    return `"${value.replaceAll('"', '""')}"`;
  }
  return value;
}

function csvRow(cells: (string | number | null)[]): string {
  return cells.map((cell) => csvCell(cell === null ? "" : String(cell))).join(",");
}

function mdCell(value: string): string {
  return value.replaceAll("|", "\\|").replaceAll("\n", " ");
}

function mdRow(cells: string[]): string {
  return `| ${cells.map(mdCell).join(" | ")} |`;
}

export function taskTableCsv(tasks: TaskSummary[], k: number): string {
  const rows = [
    csvRow(["task", "passed", "graded", `pass@${k}`, `pass^${k}`, "harness_errors", "notes"]),
    ...tasks.map((task) =>
      csvRow([
        task.slug,
        task.c,
        task.n,
        task.passAtK === null ? null : task.passAtK.toFixed(2),
        task.passHatK === null ? null : task.passHatK.toFixed(2),
        task.harnessErrors,
        taskNote(task, k),
      ]),
    ),
  ];
  return rows.join("\n") + "\n";
}

export function taskTableMarkdown(tasks: TaskSummary[], k: number): string {
  const rows = [
    mdRow(["Task", "Passed", "Graded", `pass@${k}`, `pass^${k}`, "Notes"]),
    mdRow(["---", "---:", "---:", "---:", "---:", "---"]),
    ...tasks.map((task) =>
      mdRow([
        task.slug,
        String(task.c),
        String(task.n),
        fmt(task.passAtK),
        fmt(task.passHatK),
        taskNote(task, k),
      ]),
    ),
  ];
  return rows.join("\n") + "\n";
}

export function comparisonCsv(comparison: Comparison): string {
  const rows = [
    csvRow(["task", "before", "after", "delta"]),
    ...comparison.tasks.map((task) =>
      csvRow([
        task.slug,
        task.before === null ? null : task.before.toFixed(2),
        task.after === null ? null : task.after.toFixed(2),
        task.delta === null ? null : task.delta.toFixed(2),
      ]),
    ),
  ];
  return rows.join("\n") + "\n";
}

export function comparisonMarkdown(comparison: Comparison): string {
  const rows = [
    mdRow(["Task", "Before", "After", "Change"]),
    mdRow(["---", "---:", "---:", "---:"]),
    ...comparison.tasks.map((task) =>
      mdRow([task.slug, fmt(task.before), fmt(task.after), fmtSigned(task.delta)]),
    ),
  ];
  if (comparison.kMismatch) {
    rows.push("", "The runs used different k, so per-task scores are not strictly comparable.");
  }
  if (comparison.unmatched.length > 0) {
    rows.push("", mdCell(`Only in one run: ${comparison.unmatched.join(", ")}`));
  }
  return rows.join("\n") + "\n";
}

/** Hand a generated file to the browser's download flow. */
export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
