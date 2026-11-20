// Phase 3B-C: the write-failure policy. Every failure keeps the input; only stale, limit and
// uncertain outcomes re-read the list (once); nothing is ever resent.
import { ApiError, type ApiErrorCode } from "./api";
import { UNAUTHORIZED_MESSAGE, UNCERTAIN_MESSAGE, writeOutcome } from "./write-outcome";

describe("writeOutcome", () => {
  it.each([
    ["conflict", 409, "stale", true],
    ["not_found", 404, "stale", true],
    ["version_required", 428, "stale", true],
    ["limit_reached", 409, "limit", true],
    ["duplicate_subscription", 409, "rejected", false],
    ["invalid_target_price", 400, "rejected", false],
    ["invalid_departure_month", 400, "rejected", false],
    ["invalid_version", 400, "rejected", false],
    ["bad_request", 400, "rejected", false],
    ["unauthorized", 401, "unauthorized", false],
    ["too_many_requests", 429, "rate_limited", false],
    ["network_error", 0, "uncertain", true],
    ["service_unavailable", 503, "uncertain", true],
    ["service_unavailable", 504, "uncertain", true],
    ["invalid_response", 200, "uncertain", true],
    ["internal_error", 500, "uncertain", true],
  ] as const)("%s (%i) → %s, reload=%s", (code, status, kind, reload) => {
    const out = writeOutcome(new ApiError(code as ApiErrorCode, status));
    expect(out).toMatchObject({ kind, reload, keepInput: true });
    expect(out.message).toMatch(/[一-鿿]/);
  });

  it("explains that an uncertain write may or may not have been saved", () => {
    for (const code of [
      "network_error",
      "service_unavailable",
      "invalid_response",
      "internal_error",
    ] as const) {
      expect(writeOutcome(new ApiError(code, 0)).message).toBe(UNCERTAIN_MESSAGE);
    }
    expect(UNCERTAIN_MESSAGE).toMatch(/可能已完成，也可能沒有/);
    expect(UNCERTAIN_MESSAGE).toMatch(/手動重試/);
  });

  it("treats an unknown thrown value as uncertain, never as success", () => {
    expect(writeOutcome(new Error("boom"))).toMatchObject({ kind: "uncertain", reload: true });
    expect(writeOutcome(undefined)).toMatchObject({ kind: "uncertain", reload: true });
  });

  it("asks the user to sign in again for 401", () => {
    expect(writeOutcome(new ApiError("unauthorized", 401)).message).toBe(UNAUTHORIZED_MESSAGE);
  });
});
