import { useId, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ORIGINS, POLICY, type Airport } from "@/lib/m14/catalog";
import { effectiveMonth, isExpiredMonth, monthLabel, selectableMonths } from "@/lib/m14/months";
import {
  ROLLING,
  errorMessage,
  validate,
  type ErrorCode,
  type RouteSubscription,
  type Validated,
} from "@/lib/m14/subscriptions";
import { DestinationPicker } from "./DestinationPicker";

const selectClass =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-base shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm";

function placeOptions(list: readonly Airport[]) {
  return list.map((a) => (
    <option key={a.code} value={a.code}>
      {a.name_zh} {a.name_en}（{a.code}）
    </option>
  ));
}

interface RouteFormProps {
  subs: readonly RouteSubscription[];
  now: Date;
  /** Present when editing an existing subscription. */
  editing?: RouteSubscription;
  submitLabel: string;
  /** May return a promise; resolving to false (a server-side failure) keeps the form as typed. */
  onSubmit: (value: Validated) => void | boolean | Promise<boolean>;
  onCancel?: () => void;
  /** Disables the form while a request is in flight. */
  busy?: boolean;
  /** Disables submitting (for example when the subscription limit is reached). */
  disabled?: boolean;
}

export function RouteForm({
  subs,
  now,
  editing,
  submitLabel,
  onSubmit,
  onCancel,
  busy = false,
  disabled = false,
}: RouteFormProps) {
  const id = useId();
  const months = selectableMonths(now);
  const [origin, setOrigin] = useState(editing?.origin ?? "");
  const [destination, setDestination] = useState(editing?.destination ?? "");
  const [month, setMonth] = useState(
    editing ? (editing.departure_month ?? ROLLING) : (months[0] ?? ""),
  );
  const [targetText, setTargetText] = useState(editing ? String(editing.target_price) : "");
  const [errors, setErrors] = useState<ErrorCode[]>([]);

  // Keeping a row's current value is always offered: the legacy rolling mode, or a fixed month
  // that is no longer in the window (for example an expired one).
  const keepRolling = editing !== undefined && editing.departure_month === null;
  const keepMonth =
    editing?.departure_month && !months.includes(editing.departure_month)
      ? editing.departure_month
      : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || disabled) return;
    const input = { origin, destination, month, targetText };
    const result = validate(subs, input, editing ? { editing, now } : { now });
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    setErrors([]);
    const ok = await onSubmit(result.value);
    if (ok !== false && !editing) {
      setOrigin("");
      setDestination("");
      setMonth(months[0] ?? "");
      setTargetText("");
    }
  }

  const has = (...codes: ErrorCode[]) => codes.some((c) => errors.includes(c));

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-label={editing ? "編輯訂閱" : "新增訂閱"}
      className="space-y-4"
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor={`${id}-origin`}>出發地</Label>
          <select
            id={`${id}-origin`}
            className={selectClass}
            value={origin}
            onChange={(e) => setOrigin(e.target.value)}
            aria-invalid={has("origin_required", "unsupported_origin", "same_origin_destination")}
          >
            <option value="">請選擇出發地</option>
            {placeOptions(ORIGINS)}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-destination`}>目的地</Label>
          <DestinationPicker
            id={`${id}-destination`}
            value={destination}
            onChange={setDestination}
            invalid={has(
              "destination_required",
              "unsupported_destination",
              "same_origin_destination",
            )}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-month`}>出發月份</Label>
          <select
            id={`${id}-month`}
            className={selectClass}
            value={month}
            onChange={(e) => setMonth(e.target.value)}
            aria-invalid={has("invalid_departure_month")}
          >
            {keepRolling && (
              <option value={ROLLING}>
                每月滾動：下個月（目前為 {monthLabel(effectiveMonth(null, now))}）
              </option>
            )}
            {keepMonth && (
              <option value={keepMonth}>
                {monthLabel(keepMonth)}
                {isExpiredMonth(keepMonth, now) ? "（已過期）" : ""}
              </option>
            )}
            {months.map((m) => (
              <option key={m} value={m}>
                {monthLabel(m)}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-target`}>目標價（新台幣）</Label>
          <Input
            id={`${id}-target`}
            inputMode="numeric"
            placeholder="例如 9000"
            value={targetText}
            onChange={(e) => setTargetText(e.target.value)}
            aria-invalid={has("invalid_target_price")}
          />
        </div>
      </div>

      {keepRolling && month === ROLLING && (
        <p className="text-sm text-muted-foreground">
          這是 M1 既有的訂閱，會一直查詢「下個月」出發的票價。改選固定月份後，就無法再改回每月滾動。
        </p>
      )}

      {errors.length > 0 && (
        <ul
          role="alert"
          className="space-y-1 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm"
        >
          {errors.map((code) => (
            <li key={code}>
              {errorMessage(code, {
                origin,
                destination,
                departure_month: month === ROLLING ? null : month,
                now,
                policy: POLICY,
              })}
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={busy || disabled} aria-busy={busy}>
          {busy ? "處理中…" : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
            取消
          </Button>
        )}
      </div>
    </form>
  );
}
