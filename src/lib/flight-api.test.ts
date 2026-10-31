import { describe, expect, it } from "vitest";

import { PLANS, parseTargetPrice } from "@/lib/flight-api";

describe("parseTargetPrice", () => {
  it.each([
    ["10000", 10000],
    ["10,000", 10000],
    [" 9 325 ", 9325],
    ["1", 1],
    ["1000000", 1000000],
  ])("accepts %j", (input, expected) => {
    expect(parseTargetPrice(input)).toBe(expected);
  });

  it.each(["", "0", "-5", "12.5", "abc", "1000001", "1e4"])("rejects %j", (input) => {
    expect(parseTargetPrice(input)).toBeNull();
  });
});

describe("PLANS", () => {
  it("matches the routes the API stores", () => {
    expect(PLANS.map((p) => [p.planName, p.route])).toEqual([
      ["tokyo", "TPE-TYO"],
      ["seoul", "TPE-SEL"],
    ]);
  });
});
