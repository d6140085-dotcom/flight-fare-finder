import { describe, expect, it } from "vitest";

// Same golden vectors as aws/tests/test_m14_search_links.py (spec §7.1 L01: one shared file).
import vectors from "../../../aws/tests/vectors/search-links.json";
import { DESTINATIONS, ORIGINS } from "./catalog";
import {
  ALLOWED_QUERY,
  FALLBACK_NOTICE,
  HOSTS,
  PLATFORMS,
  buildSearchLinks,
  checkUrl,
  parseDay,
  taipeiToday,
  type Day,
  type LinkSet,
} from "./search-links";

const TODAY: Day = { y: 2026, m: 11, d: 11 };
const MARKER = "000000";
const build = (
  o: unknown = "TPE",
  d: unknown = "TYO",
  dep: unknown = "2026-12-11",
  ret: unknown = "2026-12-18",
  marker: string | null = MARKER,
) => buildSearchLinks(o, d, dep, ret, TODAY, { marker });
const by = (ls: LinkSet) => Object.fromEntries(ls.links.map((l) => [l.platform, l]));

describe("search links: parity with the Phase 2B-2 Python builder", () => {
  it("reproduces every shared golden vector exactly (URL, kind and label)", () => {
    const today = parseDay(vectors.today) as Day;
    for (const c of vectors.cases) {
      const ls = buildSearchLinks(c.origin, c.destination, c.depart_date, c.return_date, today, {
        marker: vectors.marker,
      });
      expect(
        ls.links.map((l) => ({ platform: l.platform, kind: l.kind, url: l.url, label: l.label })),
      ).toEqual(c.links);
    }
  });

  it("covers all 3 origins × 95 destinations without throwing or leaving the allowed hosts", () => {
    expect(ORIGINS.map((o) => o.code)).toEqual(["TPE", "KHH", "RMQ"]);
    expect(DESTINATIONS).toHaveLength(95);
    for (const o of ORIGINS) {
      for (const d of DESTINATIONS) {
        const ls = build(o.code, d.code);
        expect(ls.compliant).toBe(true);
        const links = by(ls);
        expect(Object.keys(links)).toEqual(["skyscanner", "tripcom", "aviasales", "google"]);
        expect(links["aviasales"]?.kind).toBe("itinerary");
        for (const l of ls.links) expect(new URL(l.url).host).toBe(HOSTS[l.platform]);
      }
    }
  });
});

describe("route/date-preserving links vs general search pages", () => {
  it("keeps route, both dates, zh-TW and TWD for verified codes", () => {
    const links = by(build());
    expect(links["skyscanner"]?.url).toBe(
      "https://www.skyscanner.com.tw/transport/flights/tpe/tyo/261211/261218/?adultsv2=1&cabinclass=economy&market=TW&locale=zh-TW&currency=TWD",
    );
    expect(links["tripcom"]?.url).toContain("ddate=2026-12-11&rdate=2026-12-18&triptype=rt");
    expect(links["tripcom"]?.url).toContain("locale=zh-TW&curr=TWD");
    expect(links["aviasales"]?.url).toBe(
      "https://www.aviasales.com/search/TPE1112TYO18121?marker=000000",
    );
    expect(links["skyscanner"]?.label).toBe("在 Skyscanner 搜尋此行程");
  });

  it("labels unverified platforms as general search pages that carry no itinerary", () => {
    const links = by(build("KHH", "BKK"));
    for (const p of ["skyscanner", "tripcom", "google"]) {
      const l = links[p];
      expect(l?.kind).toBe("fallback");
      expect(l?.label).toContain("一般搜尋頁");
      expect(l?.label).toContain(FALLBACK_NOTICE);
      expect(l?.label).not.toContain("搜尋此行程");
      expect(l?.url).not.toMatch(/bkk|2026/i);
    }
    expect(links["aviasales"]?.kind).toBe("itinerary");
  });

  it("gives general pages for missing, malformed, past, out-of-window or reversed dates (never one-way)", () => {
    for (const [dep, ret] of [
      [null, "2026-12-18"],
      ["2026-12-11", null],
      ["2026-02-30", "2026-12-18"],
      ["20261211", "20261218"],
      ["2026-11-10", "2026-12-18"],
      ["2026-12-11", "2027-12-12"],
      ["2026-12-18", "2026-12-11"],
    ] as const) {
      const ls = build("TPE", "TYO", dep, ret);
      expect(ls.itineraryValid).toBe(false);
      expect(ls.compliant).toBe(true);
      for (const l of ls.links) {
        expect(l.kind).toBe("fallback");
        expect(l.url).not.toMatch(/search\/|ddate|transport\/flights/);
      }
    }
  });

  it("falls back on Aviasales when the year-less DDMM path would be ambiguous", () => {
    const links = by(build("TPE", "TYO", "2027-10-01", "2027-11-20"));
    expect(links["aviasales"]?.url).toBe("https://www.aviasales.com/?marker=000000");
    expect(links["skyscanner"]?.kind).toBe("itinerary");
  });

  it("rejects unsupported codes with no links", () => {
    for (const [o, d] of [
      ["ZZZ", "TYO"],
      ["tpe", "TYO"],
      ["TSA", "TYO"],
      ["", "TYO"],
      ["TPE", "TYOO"],
      ['"><script>', "TYO"],
      [null, "TYO"],
    ]) {
      expect(build(o, d).links).toEqual([]);
    }
  });
});

describe("affiliate links fail closed", () => {
  it("builds no links at all without a valid marker (no plain Aviasales link)", () => {
    for (const marker of [null, "", "abc", "12", "123.sub", "1234567&x=1", "1".repeat(13)]) {
      const ls = build("TPE", "TYO", "2026-12-11", "2026-12-18", marker);
      expect(ls.links).toEqual([]);
      expect(ls.compliant).toBe(false);
      expect(ls.reasons).toContain("no_valid_aviasales_marker");
    }
  });

  it("builds nothing when the Aviasales affiliate entry is switched off", () => {
    const platforms = PLATFORMS.map((p) =>
      p.id === "aviasales" ? { ...p, affiliate: { enabled: false } } : p,
    );
    const ls = buildSearchLinks("TPE", "TYO", "2026-12-11", "2026-12-18", TODAY, {
      marker: MARKER,
      platforms,
    });
    expect(ls).toMatchObject({ links: [], compliant: false });
  });

  it("puts the marker only on Aviasales, exactly once, and nothing personal in any URL", () => {
    for (const o of ["TPE", "KHH", "RMQ"]) {
      for (const d of ["TYO", "OKA", "LON", "BKK"]) {
        for (const l of build(o, d).links) {
          const u = new URL(l.url);
          const keys = [...u.searchParams.keys()];
          expect(keys.every((k) => ALLOWED_QUERY[l.platform].has(k))).toBe(true);
          expect(u.searchParams.getAll("marker")).toEqual(
            l.platform === "aviasales" ? [MARKER] : [],
          );
          expect(l.url).not.toMatch(/@|%40|eyJ|email|token|user/i);
        }
      }
    }
  });

  it("refuses tampered URLs", () => {
    const sky = by(build())["skyscanner"];
    expect(sky).toBeDefined();
    if (!sky) return;
    expect(() => checkUrl({ ...sky, url: `${sky.url}&marker=1` }, MARKER)).toThrow();
    expect(() =>
      checkUrl({ ...sky, url: "https://www.skyscanner.com.tw.evil.example/" }, MARKER),
    ).toThrow();
    expect(() => checkUrl({ ...sky, url: `${sky.url}&email=a%40example.com` }, MARKER)).toThrow();
  });
});

describe("dates", () => {
  it("uses the Taipei calendar date", () => {
    expect(taipeiToday(new Date("2026-11-10T16:30:00Z"))).toEqual({ y: 2026, m: 11, d: 11 });
    expect(taipeiToday(new Date("2026-11-10T15:30:00Z"))).toEqual({ y: 2026, m: 11, d: 10 });
  });
});
