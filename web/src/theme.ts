export type Theme = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "meridian.dashboard.theme";

const THEMES: readonly Theme[] = ["system", "light", "dark"];

export function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}

/** The last chosen theme, or "system" when nothing valid is stored. */
export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function saveTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Quota or blocked storage: the choice still applies to this visit.
  }
}

/**
 * Apply a theme to the document: a data attribute selects the palette in
 * styles.css, and "system" removes it so the media query takes over.
 */
export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  if (theme === "system") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = theme;
  }
}
