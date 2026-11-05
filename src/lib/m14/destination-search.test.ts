import { describe, expect, it } from "vitest";

import { DESTINATIONS, REGIONS } from "./catalog";
import { destinationLabel, matchRank, normalize, searchDestinations } from "./destination-search";

const codes = (q: string) =>
  searchDestinations(q).groups.flatMap((g) => g.options.map((a) => a.code));

describe("destination catalog", () => {
  it("has 80–100 unique, well-formed destinations, all original Asian ones kept", () => {
    const all = DESTINATIONS.map((a) => a.code);
    expect(all.length).toBeGreaterThanOrEqual(80);
    expect(all.length).toBeLessThanOrEqual(100);
    expect(new Set(all).size).toBe(all.length);
    for (const a of DESTINATIONS) {
      expect(a.code).toMatch(/^[A-Z]{3}$/);
      expect(a.name_zh.trim()).not.toBe("");
      expect(a.name_en.trim()).not.toBe("");
      expect(REGIONS.map((r) => r.id)).toContain(a.region);
      if (a.type === "city") expect(a.airports?.length).toBeGreaterThan(0);
      else expect(a.airports).toBeUndefined();
    }
    expect(all.slice(0, 25)).toEqual(
      "TYO OSA NGO FUK OKA SPK KMJ SEL PUS CJU HKG MFM BKK CNX HKT SIN KUL PEN BKI MNL CEB SGN HAN DAD DPS".split(
        " ",
      ),
    );
  });

  it("distinguishes city codes from airport codes", () => {
    const byCode = new Map(DESTINATIONS.map((a) => [a.code, a]));
    expect(byCode.get("LON")).toMatchObject({
      type: "city",
      airports: expect.arrayContaining(["LHR", "LGW"]),
    });
    expect(byCode.get("NYC")).toMatchObject({ type: "city", airports: ["JFK", "EWR", "LGA"] });
    expect(byCode.get("FRA")).toMatchObject({ type: "airport" });
    expect(byCode.get("AKL")).toMatchObject({ type: "airport" });
  });
});

describe("region grouping", () => {
  it("groups every destination by region, in the configured order", () => {
    const { groups } = searchDestinations("");
    expect(groups.map((g) => g.region.name_zh)).toEqual([
      "亞洲",
      "中東",
      "歐洲",
      "北美洲",
      "中南美洲",
      "澳洲",
      "紐西蘭",
    ]);
    expect(groups.reduce((n, g) => n + g.options.length, 0)).toBe(DESTINATIONS.length);
    const region = (id: string) =>
      groups.find((g) => g.region.id === id)!.options.map((a) => a.code);
    expect(region("europe")).toEqual(expect.arrayContaining(["LON", "PAR", "IST"]));
    expect(region("north_america")).toEqual(expect.arrayContaining(["NYC", "YVR", "MEX", "CUN"]));
    expect(region("latin_america")).toEqual(expect.arrayContaining(["SAO", "BUE"]));
    expect(region("australia")).toEqual(["SYD", "MEL", "BNE", "PER", "ADL", "CNS", "OOL"]);
    expect(region("new_zealand")).toEqual(["AKL", "CHC", "WLG", "ZQN"]);
  });

  it("hides regions with no match", () => {
    expect(searchDestinations("Sydney").groups.map((g) => g.region.id)).toEqual(["australia"]);
  });
});

describe("search", () => {
  it.each([
    ["倫敦", "LON"],
    ["巴黎", "PAR"],
    ["紐約", "NYC"],
    ["雪梨", "SYD"],
    ["皇后鎮", "ZQN"],
    ["夏威夷", "HNL"],
    ["東京", "TYO"],
  ])("finds %s by its Traditional Chinese name", (q, code) => {
    expect(searchDestinations(q).best?.code).toBe(code);
  });

  it.each([
    ["London", "LON"],
    ["paris", "PAR"],
    ["new york", "NYC"],
    ["NewYork", "NYC"],
    ["sao paulo", "SAO"],
    ["São Paulo", "SAO"],
    ["cancun", "CUN"],
    ["Christchurch", "CHC"],
  ])("finds %s by its English name (case and accents ignored)", (q, code) => {
    expect(searchDestinations(q).best?.code).toBe(code);
  });

  it.each([
    ["LON", "LON"],
    ["syd", "SYD"],
    ["akl", "AKL"],
    ["YTO", "YTO"],
    ["JFK", "NYC"], // an airport inside a city code finds the city
    ["LHR", "LON"],
    ["HND", "TYO"],
  ])("finds %s by IATA code", (q, code) => {
    expect(searchDestinations(q).best?.code).toBe(code);
  });

  it("ranks an exact code above partial name matches", () => {
    // "par" is a prefix of Paris and appears elsewhere, but PAR the code wins.
    expect(
      matchRank(
        DESTINATIONS.find((a) => a.code === "PAR")!,
        "par",
      ),
    ).toBe(0);
    expect(searchDestinations("par").best?.code).toBe("PAR");
  });

  it("prefers a prefix match over a substring match", () => {
    // Kumamoto (KMJ) contains "ma" and comes first in the catalog; Macau starts with it.
    expect(codes("ma").indexOf("KMJ")).toBeLessThan(codes("ma").indexOf("MFM"));
    expect(searchDestinations("ma").best?.code).toBe("MFM");
  });

  it("returns nothing for unknown text, and everything for blank input", () => {
    expect(searchDestinations("Atlantis")).toEqual({ groups: [], best: null });
    expect(codes("   ")).toHaveLength(DESTINATIONS.length);
  });

  it("normalizes text", () => {
    expect(normalize("  São  Paulo ")).toBe("sao paulo");
    expect(normalize("Washington, D.C.")).toBe("washington d c");
  });

  it("labels a destination with both names and the code", () => {
    expect(destinationLabel(DESTINATIONS.find((a) => a.code === "AKL")!)).toBe(
      "奧克蘭 Auckland（AKL）",
    );
  });
});

describe("country and region search", () => {
  it.each([
    ["日本", ["TYO", "OSA", "NGO", "FUK", "OKA", "SPK", "KMJ"]],
    ["紐西蘭", ["AKL", "CHC", "WLG", "ZQN"]],
    ["Canada", ["YVR", "YYC", "YTO", "YMQ"]],
    ["澳洲", ["SYD", "MEL", "BNE", "PER", "ADL", "CNS", "OOL"]],
  ])("lists every destination in %s", (q, expected) => {
    expect(codes(q)).toEqual(expected);
  });

  it("ranks a city name above a country or region match", () => {
    // "Hong Kong" is both a city and a country/territory name; the city entry wins either way.
    expect(searchDestinations("香港").best?.code).toBe("HKG");
    expect(
      matchRank(
        DESTINATIONS.find((a) => a.code === "AKL")!,
        "紐西蘭",
      ),
    ).toBe(3);
  });

  it("has a country name for every destination", async () => {
    const { COUNTRIES } = await import("./catalog");
    for (const a of DESTINATIONS) expect(COUNTRIES[a.country]?.name_zh, a.code).toBeTruthy();
  });
});
