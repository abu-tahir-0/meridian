import type { TaskSummary } from "../analysis";
import { fmt } from "../format";

export function TaskTable({ tasks, k }: { tasks: TaskSummary[]; k: number }) {
  return (
    <section aria-label="Per-task results">
      <h2>Tasks</h2>
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
              <th scope="col" className="num">
                95% CI
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
                <td className="num">
                  {task.passRateInterval === null
                    ? "—"
                    : `[${fmt(task.passRateInterval[0])}, ${fmt(task.passRateInterval[1])}]`}
                </td>
                <td className="num">{fmt(task.passAtK)}</td>
                <td className="num">
                  <strong>{fmt(task.passHatK)}</strong>
                </td>
                <td className="muted">
                  {task.n === 0
                    ? "no gradeable trials"
                    : task.kUsed < k
                      ? `k lowered to ${task.kUsed}: only ${task.n} gradeable`
                      : ""}
                  {task.harnessErrors > 0 ? ` ${task.harnessErrors} harness error(s)` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
