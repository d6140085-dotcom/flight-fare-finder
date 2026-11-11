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
}

/** Fare line, links and last-checked time for one subscription (active rows only). */
export function fareView(sub: SubscriptionDto, now: Date, marker: string | null): FareView {
  const fare = fareDisplay(sub, now, sub.fare_check);
  if (fare.kind !== "ok" || sub.fare_check?.kind !== "ok") return { fare, links: null };
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
    return { fare: hidden, links: null };
  }
  return { fare, links };
}
