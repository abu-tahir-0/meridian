import { parseRun, type RunResult } from "./schema";

/** A run kept in this browser so a reload does not lose it. */
export interface StoredRun {
  run: RunResult;
  name: string;
}

export type Slot = "primary" | "baseline";

export const STORAGE_KEYS: Record<Slot, string> = {
  primary: "meridian.dashboard.primary",
  baseline: "meridian.dashboard.baseline",
};

/** Store one run, or drop it if the browser refuses (quota, private mode). */
export function saveRun(slot: Slot, loaded: StoredRun): void {
  let payload: string;
  try {
    payload = JSON.stringify(loaded);
  } catch {
    return;
  }
  try {
    localStorage.setItem(STORAGE_KEYS[slot], payload);
  } catch {
    try {
      localStorage.removeItem(STORAGE_KEYS[slot]);
    } catch {
      // Storage is unavailable; the page still works, it just forgets.
    }
  }
}

/** The saved run, or null when absent, unreadable or no longer valid. */
export function restoreRun(slot: Slot): StoredRun | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS[slot]);
    if (!raw) return null;
    const data: unknown = JSON.parse(raw);
    if (typeof data !== "object" || data === null) return null;
    const { name, run } = data as { name?: unknown; run?: unknown };
    if (typeof name !== "string" || run === undefined) return null;
    const parsed = parseRun(JSON.stringify(run));
    return parsed.ok ? { run: parsed.run, name } : null;
  } catch {
    return null;
  }
}

/** Forget both saved runs. */
export function clearRuns(): void {
  for (const key of Object.values(STORAGE_KEYS)) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Nothing to clear if storage is unavailable.
    }
  }
}
