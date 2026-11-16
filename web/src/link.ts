/**
 * Helpers for loading a run from a shared dashboard link.
 */

/** Resolve a `?run=` value against the page URL. Only http(s) is allowed. */
export function resolveRunUrl(raw: string, base: string): URL | null {
  try {
    const url = new URL(raw, base);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url;
  } catch {
    return null;
  }
}

/** A short, human-readable name for the fetched file. */
export function runNameFor(url: URL): string {
  const segments = url.pathname.split("/").filter(Boolean);
  const last = segments[segments.length - 1];
  if (!last) return url.hostname;
  try {
    return decodeURIComponent(last);
  } catch {
    return last;
  }
}
