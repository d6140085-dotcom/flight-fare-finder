import { describe, expect, it } from "vitest";

import {
  addMonths,
  currentMonth,
  effectiveMonth,
  isExpiredMonth,
  monthLabel,
  selectableMonths,
} from "./months";

describe("departure months", () => {
  it("uses Taipei time for the current month", () => {
    // 2026-11-30 16:30 UTC is already 2026-12-01 00:30 in Taipei.
    expect(currentMonth(new Date("2026-11-30T16:30:00Z"))).toBe("2026-12");
    expect(currentMonth(new Date("2026-11-30T15:59:59Z"))).toBe("2026-11");
  });

  it("adds months across year boundaries", () => {
    expect(addMonths("2026-11", 1)).toBe("2026-12");
    expect(addMonths("2026-12", 1)).toBe("2027-01");
    expect(addMonths("2027-01", -1)).toBe("2026-12");
    expect(addMonths("2026-11", 12)).toBe("2027-11");
  });

  it("offers the next 12 calendar months, starting with next month", () => {
    const months = selectableMonths(new Date("2026-11-05T03:00:00Z"));
    expect(months).toHaveLength(12);
    expect(months[0]).toBe("2026-12");
    expect(months.at(-1)).toBe("2027-11");
    expect(selectableMonths(new Date("2026-12-15T00:00:00Z"))[0]).toBe("2027-01");
  });

  it("resolves the effective month: fixed month, or next month for legacy rolling rows", () => {
    const now = new Date("2026-12-15T00:00:00Z");
    expect(effectiveMonth("2027-04", now)).toBe("2027-04");
    expect(effectiveMonth(null, now)).toBe("2027-01");
  });

  it("expires a fixed month only after it has fully passed", () => {
    const now = new Date("2026-12-01T00:30:00+08:00");
    expect(isExpiredMonth("2026-11", now)).toBe(true);
    expect(isExpiredMonth("2026-12", now)).toBe(false);
    expect(isExpiredMonth(null, now)).toBe(false);
  });

  it("labels months in Chinese", () => {
    expect(monthLabel("2027-03")).toBe("2027 年 3 月");
  });
});
