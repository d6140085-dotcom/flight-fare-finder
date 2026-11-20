// M1.4 Phase 3B-C: what the page does after a failed create, edit, pause/resume or delete.
//
// One rule for every write: a failed write is never retried or replayed (no fresh version is
// fetched to resend it). `reload` only re-reads the list, once, so the user can see the latest
// server state and decide whether to try again by hand. The form keeps what the user typed.
import { ApiError, apiErrorMessage, type ApiErrorCode } from "./api";

export type WriteOutcomeKind =
  | "stale" // 409 conflict / 404 not_found / 428 version_required
  | "limit" // 409 limit_reached
  | "rejected" // the server refused the input; nothing was saved
  | "uncertain" // the write may or may not have been saved
  | "unauthorized"
  | "rate_limited";

export interface WriteOutcome {
  kind: WriteOutcomeKind;
  /** Re-read the subscription list once (never resend the write). */
  reload: boolean;
  /** Keep the user's input in the form. */
  keepInput: true;
  message: string;
}

const STALE: ReadonlySet<ApiErrorCode> = new Set(["conflict", "not_found", "version_required"]);
const UNCERTAIN: ReadonlySet<ApiErrorCode> = new Set([
  "network_error",
  "service_unavailable",
  "invalid_response",
  "internal_error",
]);

export const UNCERTAIN_MESSAGE =
  "無法確認這次變更是否已儲存：可能已完成，也可能沒有。已重新載入最新的訂閱資料，請先確認列表，再決定是否手動重試。";
export const UNAUTHORIZED_MESSAGE = "登入已過期，請重新登入後再試。";

export function writeOutcome(
  err: unknown,
  ctx: { origin?: string; destination?: string; now?: Date } = {},
): WriteOutcome {
  const code: ApiErrorCode = err instanceof ApiError ? err.code : "internal_error";
  if (UNCERTAIN.has(code) || !(err instanceof ApiError)) {
    return { kind: "uncertain", reload: true, keepInput: true, message: UNCERTAIN_MESSAGE };
  }
  if (code === "unauthorized") {
    return { kind: "unauthorized", reload: false, keepInput: true, message: UNAUTHORIZED_MESSAGE };
  }
  const message = apiErrorMessage(err, ctx);
  if (STALE.has(code)) return { kind: "stale", reload: true, keepInput: true, message };
  if (code === "limit_reached") return { kind: "limit", reload: true, keepInput: true, message };
  if (code === "too_many_requests") {
    return { kind: "rate_limited", reload: false, keepInput: true, message };
  }
  return { kind: "rejected", reload: false, keepInput: true, message };
}
