import { describe, expect, it } from "vitest";

// B6-1 offline matrix, shared with aws/tests/test_b6_link_matrix.py (one vector file). Every case
// fixes its own `now`, so nothing depends on the wall clock.
import matrix from "../../../aws/tests/vectors/search-links-matrix.json";
import type { SubscriptionDto } from "./api";
import { fareView } from "./fare-view";
import { effectiveMonth } from "./months";
import {
  FALLBACK_NOTICE,
  PLATFORMS,
  buildSearchLinks,
  checkUrl,
  monthProblem,
  taipeiToday,
  type SearchLink,
} from "./search-links";

interface MatrixCase {
  id: string;
  now: string;
  origin: string;
  destination: string;
  depart_date: string | null;
  return_date: string | null;
  subscription?: { departure_month: string | null };
  month?: string | null;
  marker?: string | null;
  disable?: string[];
  unverify?: string[];
  expect: { compliant: boolean; itinerary_valid: boolean; reasons: string[]; links: string[][] };
}

const CASES = matrix.cases as MatrixCase[];

// Skyscanner and Trip.com are disabled in the shipped MVP config; the expectations cover them as if authorized.
const AS_IF_AUTHORIZED = PLATFORMS.map((p) =>
  p.id === "skyscanner" || p.id === "tripcom"
    ? { ...p, enabled: true }
    : p.id === "aviasales"
      ? { ...p, role: "source" }
      : p,
);

const platformsFor = (
  c: MatrixCase,
  base: readonly (typeof PLATFORMS)[number][] = AS_IF_AUTHORIZED,
) =>
  base.map((p) => ({
    ...p,
    enabled: c.disable?.includes(p.id) ? false : p.enabled,
    verified_codes: c.unverify?.includes(p.id) ? { codes: [] as string[] } : p.verified_codes,
  }));

const markerOf = (c: MatrixCase) => ("marker" in c ? (c.marker ?? null) : matrix.default_marker);

const run = (c: MatrixCase, base?: readonly (typeof PLATFORMS)[number][]) =>
  buildSearchLinks(
    c.origin,
    c.destination,
    c.depart_date,
    c.return_date,
    taipeiToday(new Date(c.now)),
    { marker: markerOf(c), platforms: platformsFor(c, base), month: c.month },
  );

function datesIn(l: SearchLink, dep: string, ret: string): boolean {
  const d = dep.replace(/-/g, "");
  const r = ret.replace(/-/g, "");
  if (l.platform === "skyscanner") return l.url.includes(`/${d.slice(2)}/${r.slice(2)}/`);
  if (l.platform === "tripcom") {
    const q = new URL(l.url).searchParams;
    return q.get("ddate") === dep && q.get("rdate") === ret && q.get("triptype") === "rt";
  }
  if (l.platform === "aviasales") {
    return (
      l.url.includes(`${d.slice(6)}${d.slice(4, 6)}`) &&
      l.url.includes(`${r.slice(6)}${r.slice(4, 6)}1`)
    );
  }
  return false;
}

describe("B6-1 shared matrix (parity with the Python builder)", () => {
  it("covers 3 origins × 12 fixed months and every failure reason", () => {
    const fixed = new Set(
      CASES.filter((c) => c.id.startsWith("A-") && c.destination === "TYO").map(
        (c) => `${c.origin}#${c.month}`,
      ),
    );
    expect(fixed.size).toBe(36);
    const reasons = new Set(CASES.flatMap((c) => c.expect.reasons));
    for (const r of [
      "fare_outside_subscribed_month",
      "invalid_subscribed_month",
      "outside_window",
      "unsupported_code",
      "no_valid_aviasales_marker",
      "aviasales_affiliate_disabled",
    ]) {
      expect(reasons.has(r), r).toBe(true);
    }
  });

  it("resolves each subscription's effective month the same way as the vectors", () => {
    for (const c of CASES) {
      if (!c.subscription) continue;
      expect(effectiveMonth(c.subscription.departure_month, new Date(c.now)), c.id).toBe(c.month);
    }
  });

  it("matches every expected outcome exactly", () => {
    for (const c of CASES) {
      const ls = run(c);
      expect(
        {
          compliant: ls.compliant,
          itinerary_valid: ls.itineraryValid,
          reasons: ls.reasons,
          links: ls.links.map((l) => [l.platform, l.kind]),
        },
        c.id,
      ).toEqual(c.expect);
    }
  });

  it("gives the same outcome without Skyscanner and Trip.com under the shipped MVP config", () => {
    for (const c of CASES) {
      const ls = run(c, PLATFORMS);
      expect(
        {
          compliant: ls.compliant,
          itinerary_valid: ls.itineraryValid,
          reasons: ls.reasons,
          links: ls.links.map((l) => [l.platform, l.kind]),
        },
        c.id,
      ).toEqual({
        ...c.expect,
        links: c.expect.links.filter((pk) => pk[0] !== "tripcom" && pk[0] !== "skyscanner"),
      });
    }
  });

  it("puts the fare dates in itinerary links only, labels general pages, keeps URLs safe", () => {
    for (const c of CASES) {
      const ls = run(c);
      for (const l of ls.links) {
        checkUrl(l, markerOf(c));
        const markers = new URL(l.url).searchParams.getAll("marker");
        expect(markers, c.id).toEqual(l.platform === "aviasales" ? [markerOf(c)] : []);
        if (l.kind === "itinerary") {
          expect(datesIn(l, c.depart_date as string, c.return_date as string), c.id).toBe(true);
          if (c.month != null) expect((c.depart_date as string).slice(0, 7), c.id).toBe(c.month);
          expect(l.notice).toBeNull();
        } else {
          expect(l.notice).toBe(FALLBACK_NOTICE);
          expect(l.label).toContain("一般搜尋頁");
          expect(l.label).not.toContain("搜尋此行程");
        }
      }
    }
  });

  it("never gives an itinerary link for a fare outside the subscribed month", () => {
    const mismatches = CASES.filter((c) =>
      c.expect.reasons.includes("fare_outside_subscribed_month"),
    );
    expect(mismatches.length).toBeGreaterThanOrEqual(8);
    for (const c of mismatches) {
      const ls = run(c);
      expect(ls.itineraryValid).toBe(false);
      expect(
        ls.links.every((l) => l.kind === "fallback"),
        c.id,
      ).toBe(true);
      if (ls.links.length) expect(ls.compliant).toBe(true);
    }
  });

  it("compares year and month; no month means no check (compatibility)", () => {
    const d = { y: 2026, m: 12, d: 10 };
    expect(monthProblem(d, "2026-12")).toBeNull();
    expect(monthProblem(d, null)).toBeNull();
    expect(monthProblem(d, undefined)).toBeNull();
    expect(monthProblem(null, "2026-12")).toBeNull();
    expect(monthProblem(d, "2027-12")).toBe("fare_outside_subscribed_month");
    expect(monthProblem(d, "2026-11")).toBe("fare_outside_subscribed_month");
    for (const bad of ["2026-13", "", "2026-1", "x"]) {
      expect(monthProblem(d, bad)).toBe("invalid_subscribed_month");
    }
  });
});

describe("B6-1 subscription page uses the API's effective month", () => {
  const NOW = new Date("2026-11-23T04:00:00Z");
  const sub = (effective: string, dep: string, ret: string): SubscriptionDto => ({
    id: "TPE-TYO_2026-12",
    route: "TPE-TYO",
    origin: "TPE",
    destination: "TYO",
    departure_month: "2026-12",
    target_price: 9000,
    currency: "TWD",
    alerts_paused: false,
    plan_name: null,
    created_at: null,
    updated_at: null,
    origin_name: null,
    destination_name: null,
    month_mode: "fixed",
    effective_month: effective,
    status: "active",
    fare_check: {
      kind: "ok",
      cheapest_twd: 8062,
      checked_at: "2026-11-23T03:00:00Z",
      currency: "TWD",
      depart_date: dep,
      return_date: ret,
    },
    version: "v1",
  });

  it("keeps itinerary links when the fare departs in the effective month", () => {
    const view = fareView(sub("2026-12", "2026-12-11", "2026-12-18"), NOW, "000000");
    expect(view.links?.links[0]?.kind).toBe("itinerary");
  });

  it("shows general search pages (still with the labelled affiliate link) for another month", () => {
    const view = fareView(sub("2026-12", "2027-01-11", "2027-01-18"), NOW, "000000");
    expect(view.links?.compliant).toBe(true);
    expect(view.links?.reasons).toEqual(["fare_outside_subscribed_month"]);
    expect(view.links?.links.every((l) => l.kind === "fallback")).toBe(true);
  });
});
