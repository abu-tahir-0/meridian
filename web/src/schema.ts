import { z } from "zod";

/**
 * The shape of a Meridian run result (`RunResult.model_dump_json()`).
 *
 * Run files are untrusted input: they are validated at the boundary, and every
 * field added after the first release (such as `error_cause`) is optional so
 * older results still load.
 */
export const OutcomeSchema = z.enum(["pass", "fail", "timeout", "harness_error"]);
export type Outcome = z.infer<typeof OutcomeSchema>;

export const ErrorCauseSchema = z.enum([
  "sandbox",
  "adapter_config",
  "entrypoint",
  "grader",
  "model_provider",
  "internal",
]);
export type ErrorCause = z.infer<typeof ErrorCauseSchema>;

const EfficiencySchema = z
  .object({
    turns: z.number().default(0),
    tool_calls: z.number().default(0),
    input_tokens: z.number().default(0),
    output_tokens: z.number().default(0),
    duration_ms: z.number().default(0),
  })
  .default({ turns: 0, tool_calls: 0, input_tokens: 0, output_tokens: 0, duration_ms: 0 });

export const TrialSchema = z.object({
  task_slug: z.string(),
  trial_index: z.number().int().nonnegative(),
  outcome: OutcomeSchema,
  detail: z.string().default(""),
  attempts: z.number().int().default(1),
  efficiency: EfficiencySchema,
  error_cause: ErrorCauseSchema.nullish(),
});
export type Trial = z.infer<typeof TrialSchema>;

export const TaskResultSchema = z.object({
  task_slug: z.string(),
  trials: z.array(TrialSchema),
});
export type TaskResult = z.infer<typeof TaskResultSchema>;

// Zod drops unknown keys by default. That is the forward-compatibility promise:
// a result written by a newer Meridian still loads here, with the fields this
// build knows. `schema.test.ts` pins it so a later `.strict()` cannot ship silently.
export const RunResultSchema = z.object({
  run_id: z.string(),
  suite_slug: z.string(),
  suite_version: z.number().int(),
  status: z.string(),
  k: z.number().int().min(1),
  n_requested: z.number().int(),
  tasks: z.array(TaskResultSchema),
  excluded_task_slugs: z.array(z.string()).default([]),
  detail: z.string().default(""),
  duration_ms: z.number().default(0),
  manifest_hash: z.string().nullish(),
  cost_cents: z.number().default(0),
});
export type RunResult = z.infer<typeof RunResultSchema>;

export type ParseResult = { ok: true; run: RunResult } | { ok: false; error: string };

/** Parse untrusted text into a run, or explain why it is not one. */
export function parseRun(text: string): ParseResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { ok: false, error: "That file is not valid JSON." };
  }
  const parsed = RunResultSchema.safeParse(raw);
  if (!parsed.success) {
    const first = parsed.error.issues[0];
    const where = first && first.path.length > 0 ? ` (at ${first.path.join(".")})` : "";
    return {
      ok: false,
      error: `That JSON is not a Meridian run result${where}: ${first?.message ?? "unknown error"}.`,
    };
  }
  return { ok: true, run: parsed.data };
}
