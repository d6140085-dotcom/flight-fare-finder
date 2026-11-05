// Destination search for the M1.4 picker: Traditional Chinese name, English name (accents
// ignored) or IATA code (a city entry also matches the airport codes it covers); a country or
// region name lists all its destinations. Pure, so it can
// be tested without rendering.
import { COUNTRIES, DESTINATIONS, REGIONS, type Airport, type Region } from "./catalog";

export interface DestinationGroup {
  region: Region;
  options: Airport[];
}

/** Lower-case, strip accents ("São Paulo" → "sao paulo") and collapse spaces and punctuation. */
export function normalize(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[\s.,'’()（）-]+/g, " ")
    .trim();
}

/**
 * 0 = exact code, 1 = name or code starts with the query, 2 = name contains it,
 * 3 = its country or region name matches, null = no match.
 */
export function matchRank(a: Airport, query: string): number | null {
  const q = normalize(query);
  if (!q) return 2;
  const code = a.code.toLowerCase();
  const covered = (a.airports ?? []).map((x) => x.toLowerCase());
  if (q === code) return 0;
  const names = [normalize(a.name_zh), normalize(a.name_en)];
  if (code.startsWith(q) || names.some((n) => n.startsWith(q)) || covered.includes(q)) return 1;
  const compact = (n: string) => n.replace(/ /g, "");
  if (names.some((n) => n.includes(q) || compact(n).includes(compact(q)))) return 2;
  const country = COUNTRIES[a.country];
  const region = REGIONS.find((r) => r.id === a.region);
  const groups = [country?.name_zh, country?.name_en, region?.name_zh, region?.name_en];
  if (groups.some((g) => g && normalize(g).startsWith(q))) return 3;
  return null;
}

/**
 * Matching destinations grouped by region, in the catalog's region order. Within a group the
 * catalog order is kept; the best match is reported separately so the picker can highlight it.
 */
export function searchDestinations(
  query: string,
  list: readonly Airport[] = DESTINATIONS,
  regions: readonly Region[] = REGIONS,
): { groups: DestinationGroup[]; best: Airport | null } {
  let best: Airport | null = null;
  let bestRank = Infinity;
  const byRegion = new Map<string, Airport[]>();
  for (const a of list) {
    const rank = matchRank(a, query);
    if (rank === null) continue;
    if (rank < bestRank) {
      best = a;
      bestRank = rank;
    }
    const key = a.region ?? "";
    byRegion.set(key, [...(byRegion.get(key) ?? []), a]);
  }
  const groups = regions
    .filter((r) => byRegion.has(r.id))
    .map((region) => ({ region, options: byRegion.get(region.id)! }));
  // With an empty query the first option is the natural starting point.
  if (!normalize(query)) best = groups[0]?.options[0] ?? null;
  return { groups, best };
}

/** "東京 Tokyo（TYO）" — the text shown in the input once a destination is chosen. */
export function destinationLabel(a: Airport): string {
  return `${a.name_zh} ${a.name_en}（${a.code}）`;
}
