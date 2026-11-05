// Departure-month helpers. Months are "YYYY-MM" strings, which sort correctly as text.
// "Current month" is taken in the policy time zone (Asia/Taipei), so expiry flips at local
// midnight on the 1st, not at UTC midnight.
import { POLICY } from "./catalog";

export const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

/** The calendar month containing `now`, in `timeZone`, as "YYYY-MM". */
export function currentMonth(now: Date, timeZone: string = POLICY.timeZone): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
  }).formatToParts(now);
  const year = parts.find((p) => p.type === "year")?.value;
  const month = parts.find((p) => p.type === "month")?.value;
  return `${year}-${month}`;
}

/** Adds whole months to a "YYYY-MM" string. */
export function addMonths(month: string, n: number): string {
  const [y = 0, m = 1] = month.split("-").map(Number);
  const index = y * 12 + (m - 1) + n;
  return `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, "0")}`;
}

/** The months a user may choose: next month first, `window` months in total. */
export function selectableMonths(now: Date, policy = POLICY): string[] {
  const start = addMonths(currentMonth(now, policy.timeZone), policy.firstMonthOffset);
  return Array.from({ length: policy.monthWindow }, (_, i) => addMonths(start, i));
}

/** The month a fare check uses: the fixed month, or next month for legacy rolling rows. */
export function effectiveMonth(departureMonth: string | null, now: Date, policy = POLICY): string {
  return departureMonth ?? addMonths(currentMonth(now, policy.timeZone), 1);
}

/** A fixed month has expired once that whole calendar month is in the past. */
export function isExpiredMonth(departureMonth: string | null, now: Date, policy = POLICY): boolean {
  return departureMonth !== null && departureMonth < currentMonth(now, policy.timeZone);
}

/** "2026 年 12 月". */
export function monthLabel(month: string): string {
  const [y, m] = month.split("-");
  return `${y} 年 ${Number(m)} 月`;
}
