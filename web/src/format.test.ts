import { describe, expect, it } from "vitest";
import { fmt, fmtCost, fmtDuration, fmtPercent, fmtSigned } from "./format";

describe("fmt", () => {
  it("renders a missing value as an em dash", () => {
    expect(fmt(null)).toBe("—");
  });

  it("uses two digits by default and honours a custom count", () => {
    expect(fmt(0)).toBe("0.00");
    expect(fmt(1.26, 1)).toBe("1.3");
    expect(fmt(-1.234)).toBe("-1.23");
  });
});

describe("fmtSigned", () => {
  it("renders a missing value as an em dash", () => {
    expect(fmtSigned(null)).toBe("—");
  });

  it("prefixes positive values with a plus", () => {
    expect(fmtSigned(0.5)).toBe("+0.50");
  });

  it("uses a typographic minus for negative values", () => {
    expect(fmtSigned(-0.5)).toBe("−0.50");
  });

  it("leaves zero unsigned", () => {
    expect(fmtSigned(0)).toBe("0.00");
  });

  it("honours a custom digit count", () => {
    expect(fmtSigned(-2.345, 1)).toBe("−2.3");
  });
});

describe("fmtPercent", () => {
  it("shows one decimal place", () => {
    expect(fmtPercent(0)).toBe("0.0%");
    expect(fmtPercent(0.1234)).toBe("12.3%");
    expect(fmtPercent(1)).toBe("100.0%");
  });

  it("keeps the sign for a negative share", () => {
    expect(fmtPercent(-0.05)).toBe("-5.0%");
  });
});

describe("fmtDuration", () => {
  it("shows sub-minute values in seconds", () => {
    expect(fmtDuration(0)).toBe("0.0s");
    expect(fmtDuration(1_500)).toBe("1.5s");
    expect(fmtDuration(89_000)).toBe("89.0s");
  });

  it("switches to minutes at 90 seconds", () => {
    expect(fmtDuration(89_999)).toBe("90.0s");
    expect(fmtDuration(90_000)).toBe("1.5m");
    expect(fmtDuration(5_399_000)).toBe("90.0m");
  });

  it("switches to hours at 90 minutes", () => {
    expect(fmtDuration(5_400_000)).toBe("1.5h");
  });
});

describe("fmtCost", () => {
  it("keeps sub-dollar amounts in cents", () => {
    expect(fmtCost(0)).toBe("0¢");
    expect(fmtCost(99)).toBe("99¢");
  });

  it("switches to dollars at 100 cents", () => {
    expect(fmtCost(100)).toBe("$1.00");
    expect(fmtCost(12_345)).toBe("$123.45");
  });

  it("keeps the sign for a negative amount", () => {
    expect(fmtCost(-1)).toBe("-1¢");
  });
});
