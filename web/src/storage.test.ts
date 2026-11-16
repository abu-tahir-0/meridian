import { beforeEach, describe, expect, it, vi } from "vitest";
import sample from "../public/sample-run.json";
import { parseRun } from "./schema";
import { clearRuns, restoreRun, saveRun, STORAGE_KEYS } from "./storage";

function loaded() {
  const parsed = parseRun(JSON.stringify(sample));
  if (!parsed.ok) throw new Error(parsed.error);
  return { run: parsed.run, name: "kept.json" };
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("saved runs", () => {
  it("round-trips through storage and clears both slots", () => {
    saveRun("primary", loaded());
    saveRun("baseline", loaded());
    expect(restoreRun("primary")?.name).toBe("kept.json");
    expect(restoreRun("baseline")?.name).toBe("kept.json");

    clearRuns();
    expect(restoreRun("primary")).toBeNull();
    expect(restoreRun("baseline")).toBeNull();
  });

  it("ignores corrupt JSON instead of throwing", () => {
    localStorage.setItem(STORAGE_KEYS.primary, "{not json");
    expect(restoreRun("primary")).toBeNull();
  });

  it("ignores a stored object that is no longer a valid run", () => {
    localStorage.setItem(STORAGE_KEYS.primary, JSON.stringify({ name: "x", run: { tasks: [] } }));
    expect(restoreRun("primary")).toBeNull();
  });

  it("survives a refused write, such as a full or private-mode store", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => saveRun("primary", loaded())).not.toThrow();
    expect(restoreRun("primary")).toBeNull();
  });
});
