// Multi-platform flight search links for the subscription page: a TypeScript port of the
// Phase 2B-2 builder (aws/lambdas/flight-fare-notification-v2/notify2/searchlinks.py), checked
// against the same golden vectors (aws/tests/vectors/search-links.json) and the same platform
// config (aws/config/search-platforms.json). Spec: docs/m1.4-multi-platform-search-links.md.
//
// Rules carried over unchanged:
// - Itinerary (route + both dates) links only for codes in a platform's verified_codes and valid
//   dates; otherwise that platform's general search page, labelled as such. Never one-way.
// - Travelpayouts condition 3: without a valid Aviasales marker there are NO links at all
//   (`compliant: false`); a plain, non-affiliate Aviasales link is never produced.
// - URL formats are fixed here; the config only switches/orders platforms and lists codes.
import platformsConfig from "../../../aws/config/search-platforms.json";
import { DESTINATIONS, ORIGINS } from "./catalog";

export type PlatformId = "skyscanner" | "tripcom" | "aviasales" | "google";
export type LinkKind = "itinerary" | "fallback";

export interface SearchLink {
  platform: PlatformId;
  label: string;
  url: string;
  kind: LinkKind;
  role: string;
  affiliate: boolean;
  notice: string | null;
}

export interface LinkSet {
  links: SearchLink[];
  itineraryValid: boolean;
  /** A labelled Aviasales affiliate link is present (Travelpayouts condition 3). */
  compliant: boolean;
  reasons: string[];
}

interface PlatformConfig {
  id: PlatformId;
  label: string;
  order: number;
  enabled: boolean;
  role: string;
  verified_codes: { codes: string[] | "catalog" };
  code_overrides: Record<string, string>;
  affiliate: { enabled: boolean };
}

export const PLATFORMS: readonly PlatformConfig[] = (
  platformsConfig.platforms as unknown as PlatformConfig[]
)
  .slice()
  .sort((a, b) => a.order - b.order);

export const FALLBACK_NOTICE = "未帶入行程，請自行輸入並確認航線與日期";
export const HOSTS: Record<PlatformId, string> = {
  skyscanner: "www.skyscanner.com.tw",
  tripcom: "tw.trip.com",
  aviasales: "www.aviasales.com",
  google: "www.google.com",
};
export const ALLOWED_QUERY: Record<PlatformId, ReadonlySet<string>> = {
  skyscanner: new Set(["adultsv2", "cabinclass", "market", "locale", "currency"]),
  tripcom: new Set([
    "dcity",
    "acity",
    "ddate",
    "rdate",
    "triptype",
    "class",
    "quantity",
    "locale",
    "curr",
  ]),
  aviasales: new Set(["marker"]),
  google: new Set(["hl"]),
};
const FALLBACK_URLS: Record<PlatformId, string> = {
  skyscanner: "https://www.skyscanner.com.tw/",
  tripcom: "https://tw.trip.com/flights/",
  aviasales: "https://www.aviasales.com/",
  google: "https://www.google.com/travel/flights?hl=zh-TW",
};
const AFFILIATE_KEYS = new Set([
  "marker",
  "mediapartnerid",
  "irclickid",
  "associateid",
  "campaign_id",
  "allianceid",
  "sid",
  "subid",
  "aff",
  "affiliate",
  "ref",
]);
const CODE_RE = /^[A-Z]{3}$/;
const MARKER_RE = /^[0-9]{3,12}$/;
const WINDOW_MONTHS = 13;
const AVIASALES_TAGS = "票價來源・聯盟連結・英文介面";

const ORIGIN_CODES = new Set(ORIGINS.map((a) => a.code));
const DEST_CODES = new Set(DESTINATIONS.map((a) => a.code));

export const validMarker = (marker: unknown): marker is string =>
  typeof marker === "string" && MARKER_RE.test(marker);

// ---- dates: plain calendar dates, no time zones involved ---------------------------------

export interface Day {
  y: number;
  m: number; // 1-12
  d: number;
}

const daysInMonth = (y: number, m: number) => new Date(Date.UTC(y, m, 0)).getUTCDate();
const cmp = (a: Day, b: Day) => a.y - b.y || a.m - b.m || a.d - b.d;
const pad = (n: number, w = 2) => String(n).padStart(w, "0");
export const iso = (x: Day) => `${pad(x.y, 4)}-${pad(x.m)}-${pad(x.d)}`;

/** "YYYY-MM-DD" (optionally followed by "T…") -> Day, else null. */
export function parseDay(value: unknown): Day | null {
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})($|T)/.exec(value);
  if (!match) return null;
  const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])];
  if (m < 1 || m > 12 || d < 1 || d > daysInMonth(y, m)) return null;
  return { y, m, d };
}

export function addMonths(day: Day, n: number): Day {
  const total = day.y * 12 + day.m - 1 + n;
  const y = Math.floor(total / 12);
  const m = (total % 12) + 1;
  return { y, m, d: Math.min(day.d, daysInMonth(y, m)) };
}

/** Today's calendar date in Asia/Taipei. */
export function taipeiToday(now: Date): Day {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value);
  return { y: get("year"), m: get("month"), d: get("day") };
}

function dateProblem(dep: Day | null, ret: Day | null, today: Day): string | null {
  if (!dep || !ret) return "missing_or_malformed_date";
  if (cmp(dep, today) < 0) return "departure_in_the_past";
  if (cmp(ret, dep) < 0) return "return_before_departure";
  if (cmp(ret, addMonths(today, WINDOW_MONTHS)) > 0) return "outside_window";
  return null;
}

// ---- per-platform itinerary URLs (spec §4.2) ----------------------------------------------

const yymmdd = (x: Day) => `${pad(x.y % 100)}${pad(x.m)}${pad(x.d)}`;
const ddmm = (x: Day) => `${pad(x.d)}${pad(x.m)}`;
const query = (params: Record<string, string>) => new URLSearchParams(params).toString();

function skyscannerUrl(o: string, d: string, dep: Day, ret: Day) {
  const path = [o.toLowerCase(), d.toLowerCase(), yymmdd(dep), yymmdd(ret)]
    .map(encodeURIComponent)
    .join("/");
  return (
    `https://www.skyscanner.com.tw/transport/flights/${path}/?` +
    query({
      adultsv2: "1",
      cabinclass: "economy",
      market: "TW",
      locale: "zh-TW",
      currency: "TWD",
    })
  );
}

function tripcomUrl(o: string, d: string, dep: Day, ret: Day) {
  return (
    "https://tw.trip.com/flights/showfarefirst?" +
    query({
      dcity: o.toLowerCase(),
      acity: d.toLowerCase(),
      ddate: iso(dep),
      rdate: iso(ret),
      triptype: "rt",
      class: "y",
      quantity: "1",
      locale: "zh-TW",
      curr: "TWD",
    })
  );
}

function aviasalesUrl(o: string, d: string, dep: Day, ret: Day, marker: string) {
  const path = encodeURIComponent(`${o}${ddmm(dep)}${d}${ddmm(ret)}1`);
  return `https://www.aviasales.com/search/${path}?${query({ marker })}`;
}

function label(p: PlatformConfig, kind: LinkKind): string {
  if (p.id === "aviasales") {
    return kind === "itinerary"
      ? `在 Aviasales 搜尋此行程（${AVIASALES_TAGS}）`
      : `前往 Aviasales 一般搜尋頁（${AVIASALES_TAGS}；${FALLBACK_NOTICE}）`;
  }
  return kind === "itinerary"
    ? `在 ${p.label} 搜尋此行程`
    : `前往 ${p.label} 一般搜尋頁（${FALLBACK_NOTICE}）`;
}

const empty = (itineraryValid: boolean, reasons: string[]): LinkSet => ({
  links: [],
  itineraryValid,
  compliant: false,
  reasons,
});

/** Links for one round-trip reference fare. Bad input never throws. */
export function buildSearchLinks(
  origin: unknown,
  destination: unknown,
  departDate: unknown,
  returnDate: unknown,
  today: Day,
  opts: { marker?: string | null | undefined; platforms?: readonly PlatformConfig[] } = {},
): LinkSet {
  if (
    typeof origin !== "string" ||
    typeof destination !== "string" ||
    !CODE_RE.test(origin) ||
    !CODE_RE.test(destination) ||
    !ORIGIN_CODES.has(origin) ||
    !DEST_CODES.has(destination)
  ) {
    return empty(false, ["unsupported_code"]);
  }
  const dep = parseDay(departDate);
  const ret = parseDay(returnDate);
  const problem = dateProblem(dep, ret, today);
  const reasons = problem ? [problem] : [];
  const marker = opts.marker;
  if (!validMarker(marker))
    return empty(problem === null, [...reasons, "no_valid_aviasales_marker"]);

  const links: SearchLink[] = [];
  for (const p of opts.platforms ?? PLATFORMS) {
    if (!p.enabled) continue;
    const vc = p.verified_codes.codes;
    const verified = vc === "catalog" || (vc.includes(origin) && vc.includes(destination));
    const o = p.code_overrides[origin] ?? origin;
    const d = p.code_overrides[destination] ?? destination;
    const itinerary = problem === null && verified && dep !== null && ret !== null;
    if (p.id === "aviasales") {
      if (!p.affiliate.enabled) continue;
      // Aviasales' path has no year: only unambiguous before the same day next year.
      if (itinerary && cmp(ret, addMonths(today, 12)) < 0) {
        links.push(link(p, "itinerary", aviasalesUrl(o, d, dep, ret, marker), true));
      } else {
        links.push(link(p, "fallback", `${FALLBACK_URLS.aviasales}?${query({ marker })}`, true));
      }
    } else if (itinerary && (p.id === "skyscanner" || p.id === "tripcom")) {
      const url = (p.id === "skyscanner" ? skyscannerUrl : tripcomUrl)(o, d, dep, ret);
      links.push(link(p, "itinerary", url, false));
    } else {
      links.push(link(p, "fallback", FALLBACK_URLS[p.id], false));
    }
  }
  if (!links.some((l) => l.platform === "aviasales" && l.affiliate)) {
    return empty(problem === null, [...reasons, "aviasales_affiliate_disabled"]);
  }
  for (const l of links) checkUrl(l, marker);
  return { links, itineraryValid: problem === null, compliant: true, reasons };
}

function link(p: PlatformConfig, kind: LinkKind, url: string, affiliate: boolean): SearchLink {
  return {
    platform: p.id,
    label: label(p, kind),
    url,
    kind,
    role: p.role,
    affiliate,
    notice: kind === "fallback" ? FALLBACK_NOTICE : null,
  };
}

/** Defence in depth: https on the platform's host, allow-listed keys, marker only on Aviasales. */
export function checkUrl(l: SearchLink, marker: string | null): void {
  const u = new URL(l.url);
  if (
    u.protocol !== "https:" ||
    u.host !== HOSTS[l.platform] ||
    u.hash ||
    u.username ||
    u.password ||
    u.port
  ) {
    throw new Error(`unsafe URL for ${l.platform}`);
  }
  const keys = [...u.searchParams.keys()];
  if (keys.some((k) => !ALLOWED_QUERY[l.platform].has(k)) || new Set(keys).size !== keys.length) {
    throw new Error(`unexpected query keys for ${l.platform}`);
  }
  if (l.platform !== "aviasales" && keys.some((k) => AFFILIATE_KEYS.has(k.toLowerCase()))) {
    throw new Error(`affiliate parameter on ${l.platform}`);
  }
  const markers = u.searchParams.getAll("marker");
  const want = l.affiliate && marker ? [marker] : [];
  if (l.platform === "aviasales" && markers.join() !== want.join()) {
    throw new Error("aviasales marker must appear exactly once, on the affiliate link");
  }
  if (/@|%40|eyJ/.test(l.url)) throw new Error("a link must never carry an address or token");
}
