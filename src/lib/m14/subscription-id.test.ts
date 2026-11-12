import { describe, expect, it } from "vitest";

// Same vectors as aws/tests/test_m14_multi_month.py (Python subkeys.py).
import vectors from "../../../aws/tests/vectors/subscription-ids.json";
import {
  idFromSortKey,
  parseSortKey,
  parseSubscriptionId,
  sortKey,
  sortKeyFromId,
  subscriptionId,
} from "./subscription-id";

describe("subscription identity (R-MM-1)", () => {
  it("matches the shared vectors exactly", () => {
    for (const c of vectors.valid) {
      expect(sortKey(c.origin, c.destination, c.departure_month)).toBe(c.sort_key);
      expect(subscriptionId(c.origin, c.destination, c.departure_month)).toBe(c.id);
      expect(idFromSortKey(c.sort_key)).toBe(c.id);
      expect(sortKeyFromId(c.id)).toBe(c.sort_key);
      expect(parseSubscriptionId(c.id)).toEqual({
        origin: c.origin,
        destination: c.destination,
        month: c.departure_month,
      });
      expect(parseSortKey(c.sort_key)).toEqual(parseSubscriptionId(c.id));
    }
  });

  it("rejects malformed IDs and keys", () => {
    for (const bad of vectors.invalid_ids) {
      expect(parseSubscriptionId(bad), bad).toBeNull();
      expect(sortKeyFromId(bad), bad).toBeNull();
    }
    for (const bad of vectors.invalid_keys) expect(parseSortKey(bad), bad).toBeNull();
    expect(parseSubscriptionId(undefined)).toBeNull();
  });

  it("produces URL-safe IDs that never need percent-encoding", () => {
    for (const c of vectors.valid) {
      expect(encodeURIComponent(c.id)).toBe(c.id);
      expect(c.id).not.toContain("#");
    }
  });
});
