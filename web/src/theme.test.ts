import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyTheme,
  isTheme,
  readTheme,
  saveTheme,
  THEME_STORAGE_KEY,
} from "./theme";

describe("theme", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  it("defaults to the system theme", () => {
    expect(readTheme()).toBe("system");
  });

  it("round-trips a saved choice and ignores invalid values", () => {
    saveTheme("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(readTheme()).toBe("dark");
    localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    expect(readTheme()).toBe("system");
    expect(isTheme("sepia")).toBe(false);
  });

  it("marks the document for a fixed theme and clears it for system", () => {
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    applyTheme("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("survives storage that refuses reads and writes", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readTheme()).toBe("system");
    vi.restoreAllMocks();

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => saveTheme("dark")).not.toThrow();
  });
});
