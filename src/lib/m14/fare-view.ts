// What a subscription row shows about its latest fare check: the fare line, the last-checked
// time and (for an active row with a reference fare) the search links. Fail closed: a reference
// amount is shown only together with the labelled Aviasales affiliate link (Travelpayouts
// condition 3, docs/m1.4-phase2b2-search-links.md §2.3).
import type { SubscriptionDto } from "./api";
import { fareDisplay, type FareDisplay } from "./fare-status";
import { buildSearchLinks, taipeiToday, type LinkSet } from "./search-links";

export const NO_AFFILIATE_TEXT =
  "已查到參考票價，但目前無法提供 Aviasales 聯盟連結，因此暫不顯示金額與搜尋連結。";

export interface FareView {
  fare: FareDisplay;
  links: LinkSet | null;
  /** The reference amount to show, or null (no valid fare, or no affiliate link: fail closed). */
  price: number | null;
  /** Reference fare vs the user's target, only when `price` is shown. */
  comparison: TargetComparison | null;
  /** The fare's round trip, when the check reported both dates and the amount is shown. */
  itinerary: { depart: string; return: string } | null;
}

export interface TargetComparison {
  kind: "below" | "equal" | "above";
  /** Absolute difference in whole TWD. */
  amount: number;
  text: string;
}

const twd = new Intl.NumberFormat("zh-TW");

/**
 * "低於目標價 NT$938". Describes the reference fare only: it never says that a notification was
 * or will be sent (sending depends on the fare check and deduplication, not on this page).
 */
export function compareToTarget(target: number, price: number): TargetComparison {
  const diff = target - price;
  if (diff > 0) return { kind: "below", amount: diff, text: `低於目標價 NT$${twd.format(diff)}` };
  if (diff < 0) return { kind: "above", amount: -diff, text: `高於目標價 NT$${twd.format(-diff)}` };
  return { kind: "equal", amount: 0, text: "等於目標價" };
}

/** Fare line, links and last-checked time for one subscription (active rows only). */
export function fareView(sub: SubscriptionDto, now: Date, marker: string | null): FareView {
  const fare = fareDisplay(sub, now, sub.fare_check);
  const none = { links: null, price: null, comparison: null, itinerary: null };
  if (fare.kind !== "ok" || sub.fare_check?.kind !== "ok") return { fare, ...none };
  const check = sub.fare_check;
  const links = buildSearchLinks(
    sub.origin,
    sub.destination,
    check.depart_date,
    check.return_date,
    taipeiToday(now),
    { marker },
  );
  if (!links.compliant) {
    // Fail closed: never a reference amount without the labelled Aviasales affiliate link.
    const hidden: FareDisplay = { kind: "ok", text: NO_AFFILIATE_TEXT };
    if (fare.checkedAt) hidden.checkedAt = fare.checkedAt;
    return { fare: hidden, ...none };
  }
  const price = check.cheapest_twd;
  return {
    fare,
    links,
    price,
    comparison: compareToTarget(sub.target_price, price),
    itinerary:
      check.depart_date && check.return_date
        ? { depart: check.depart_date, return: check.return_date }
        : null,
  };
}
