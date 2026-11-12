// Subscription identity (R-MM-1). Mirrors aws/lambdas/*/subkeys.py and is checked against the
// same vectors (aws/tests/vectors/subscription-ids.json).
//
//   legacy rolling row:  sort key "KHH-TYO"          public ID "KHH-TYO"
//   fixed-month row:     sort key "KHH-TYO#2027-01"  public ID "KHH-TYO_2027-01"
//
// The public ID is what the page uses in API paths (PATCH/DELETE /subscriptions/{id}); it is
// URL-safe and never needs percent-encoding. The page never sees or sends the "#" form.

const PUBLIC_ID_RE = /^([A-Z]{3})-([A-Z]{3})(?:_(\d{4}-(?:0[1-9]|1[0-2])))?$/;
const SORT_KEY_RE = /^([A-Z]{3})-([A-Z]{3})(?:#(\d{4}-(?:0[1-9]|1[0-2])))?$/;

export const routePair = (origin: string, destination: string) => `${origin}-${destination}`;

/** DynamoDB sort key (documentation and tests only; the page works with public IDs). */
export function sortKey(origin: string, destination: string, month: string | null): string {
  return month === null
    ? routePair(origin, destination)
    : `${routePair(origin, destination)}#${month}`;
}

export function subscriptionId(origin: string, destination: string, month: string | null): string {
  return month === null
    ? routePair(origin, destination)
    : `${routePair(origin, destination)}_${month}`;
}

export interface ParsedId {
  origin: string;
  destination: string;
  month: string | null;
}

export function parseSubscriptionId(id: unknown): ParsedId | null {
  if (typeof id !== "string") return null;
  const m = PUBLIC_ID_RE.exec(id);
  return m ? { origin: m[1] as string, destination: m[2] as string, month: m[3] ?? null } : null;
}

export function parseSortKey(key: unknown): ParsedId | null {
  if (typeof key !== "string") return null;
  const m = SORT_KEY_RE.exec(key);
  return m ? { origin: m[1] as string, destination: m[2] as string, month: m[3] ?? null } : null;
}

export const idFromSortKey = (key: string) => key.replace("#", "_");
export const sortKeyFromId = (id: string) =>
  parseSubscriptionId(id) ? id.replace("_", "#") : null;
