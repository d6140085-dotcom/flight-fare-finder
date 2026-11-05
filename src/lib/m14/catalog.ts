// M1.4 airport allowlist and subscription policy, read from the same config files the API
// will validate against (aws/config/*.json). One source of truth for front end and back end.
import airportsConfig from "../../../aws/config/airports.json";
import policyConfig from "../../../aws/config/subscription-policy.json";

export type AirportType = "city" | "airport";

export interface Airport {
  code: string;
  type: AirportType;
  name_zh: string;
  name_en: string;
  country: string;
}

export interface SubscriptionPolicy {
  maxActive: number;
  minTarget: number;
  maxTarget: number;
}

export const ORIGINS: readonly Airport[] = airportsConfig.origins as Airport[];
export const DESTINATIONS: readonly Airport[] = airportsConfig.destinations as Airport[];

export const POLICY: SubscriptionPolicy = {
  maxActive: policyConfig.max_active_subscriptions_per_user,
  minTarget: policyConfig.target_price_twd.min,
  maxTarget: policyConfig.target_price_twd.max,
};

const byCode = new Map<string, Airport>([...ORIGINS, ...DESTINATIONS].map((a) => [a.code, a]));

export function airport(code: string): Airport | undefined {
  return byCode.get(code);
}

/** "台北 TPE", or the bare code if it isn't in the allowlist. */
export function placeLabel(code: string): string {
  const a = byCode.get(code);
  return a ? `${a.name_zh} ${a.code}` : code;
}

/** "台北 → 東京" (Chinese names), falling back to codes. */
export function routeLabel(origin: string, destination: string): string {
  return `${byCode.get(origin)?.name_zh ?? origin} → ${byCode.get(destination)?.name_zh ?? destination}`;
}

/** "Taipei → Tokyo". */
export function routeLabelEn(origin: string, destination: string): string {
  return `${byCode.get(origin)?.name_en ?? origin} → ${byCode.get(destination)?.name_en ?? destination}`;
}

export const routeKey = (origin: string, destination: string) => `${origin}-${destination}`;
