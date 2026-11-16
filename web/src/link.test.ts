import { describe, expect, it } from "vitest";
import { resolveRunUrl, runNameFor } from "./link";

const base = "https://dashboard.test/meridian/";

describe("resolveRunUrl", () => {
  it("resolves absolute and relative http(s) links", () => {
    expect(resolveRunUrl("https://example.test/a/run.json", base)?.href).toBe(
      "https://example.test/a/run.json",
    );
    expect(resolveRunUrl("runs/result.json", base)?.href).toBe(
      "https://dashboard.test/meridian/runs/result.json",
    );
  });

  it("rejects schemes that are not http or https", () => {
    expect(resolveRunUrl("javascript:alert(1)", base)).toBeNull();
    expect(resolveRunUrl("data:application/json,{}", base)).toBeNull();
    expect(resolveRunUrl("file:///etc/passwd", base)).toBeNull();
  });

  it("rejects values that are not URLs at all", () => {
    expect(resolveRunUrl("http://[nope", base)).toBeNull();
  });
});

describe("runNameFor", () => {
  it("uses the last path segment, decoded", () => {
    expect(runNameFor(new URL("https://example.test/a/my%20run.json"))).toBe("my run.json");
  });

  it("falls back to the host when the path is empty", () => {
    expect(runNameFor(new URL("https://example.test"))).toBe("example.test");
    expect(runNameFor(new URL("https://example.test/"))).toBe("example.test");
  });

  it("keeps a malformed escape instead of throwing", () => {
    const url = new URL("https://example.test/%E0%A4%A");
    expect(runNameFor(url)).toBe("%E0%A4%A");
  });
});
