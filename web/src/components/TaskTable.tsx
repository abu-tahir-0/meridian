import type { TaskSummary } from "../analysis";
import { taskNote, taskTableCsv, taskTableMarkdown } from "../export";
import { fmt } from "../format";
import { ExportButtons } from "./ExportButtons";

export function TaskTable({ tasks, k }: { tasks: TaskSummary[]; k: number }) {
  return (
    <section aria-label="Per-task results">
      <h2>Tasks</h2>
      <ExportButtons
        label="tasks"
        filename="meridian-tasks.csv"
        markdown={taskTableMarkdown(tasks, k)}
        csv={taskTableCsv(tasks, k)}
      />
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Task</th>
              <th scope="col" className="num">
                Passed
              </th>
              <th scope="col" className="num">
                Graded
              </th>
              <th scope="col" className="num">{`pass@${k}`}</th>
              <th scope="col" className="num">{`pass^${k}`}</th>
              <th scope="col">Notes</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.slug}>
                <th scope="row">{task.slug}</th>
                <td className="num">{task.c}</td>
                <td className="num">{task.n}</td>
                <td className="num">{fmt(task.passAtK)}</td>
                <td className="num">
                  <strong>{fmt(task.passHatK)}</strong>
                </td>
                <td className="muted">{taskNote(task, k)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
