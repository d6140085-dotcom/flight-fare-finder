import { supabase } from "@/integrations/supabase/client";

// The two fixed M1 plans. The API (flight-save-subscription) knows the same list.
export const PLANS = [
  {
    planName: "tokyo",
    route: "TPE-TYO",
    label: "台北 ✈ 東京",
    labelEn: "Taipei → Tokyo",
    hintTwd: 9325,
  },
  {
    planName: "seoul",
    route: "TPE-SEL",
    label: "台北 ✈ 首爾",
    labelEn: "Taipei → Seoul",
    hintTwd: 5989,
  },
] as const;

export type PlanName = (typeof PLANS)[number]["planName"];

export interface Subscription {
  route: string;
  plan_name: PlanName;
  target_price: number;
  currency: "TWD";
  updated_at: string;
}

export const MAX_TARGET_TWD = 1_000_000;

/** Parses a target-price input into a whole TWD amount, or returns null if it isn't valid. */
export function parseTargetPrice(input: string): number | null {
  const cleaned = input.replace(/[,\s]/g, "");
  if (!/^\d+$/.test(cleaned)) return null;
  const value = Number(cleaned);
  return value >= 1 && value <= MAX_TARGET_TWD ? value : null;
}

function apiUrl(): string {
  const url = import.meta.env["VITE_FLIGHT_API_URL"];
  if (!url) throw new Error("Missing VITE_FLIGHT_API_URL. Set it in .env.local or Vercel.");
  return url.replace(/\/+$/, "");
}

// The API identifies the user from their Supabase session token, never from an email in the request.
async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new Error("Not signed in.");
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${apiUrl()}${path}`, { ...init, headers });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(body.error ?? `Request failed (${res.status})`);
  }
  return res;
}

export async function listSubscriptions(): Promise<Subscription[]> {
  const res = await authedFetch("/subscriptions");
  const body = (await res.json()) as { subscriptions: Subscription[] };
  return body.subscriptions;
}

export async function saveSubscription(
  planName: PlanName,
  targetPrice: number,
): Promise<Subscription> {
  const res = await authedFetch("/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_name: planName, target_price: targetPrice }),
  });
  return (await res.json()) as Subscription;
}
