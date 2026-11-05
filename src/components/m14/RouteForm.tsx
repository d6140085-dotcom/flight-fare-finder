import { useId, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DESTINATIONS, ORIGINS, POLICY, type Airport } from "@/lib/m14/catalog";
import {
  errorMessage,
  validate,
  type ErrorCode,
  type RouteSubscription,
  type Validated,
} from "@/lib/m14/subscriptions";

const selectClass =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-base shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm";

function options(list: readonly Airport[]) {
  return list.map((a) => (
    <option key={a.code} value={a.code}>
      {a.name_zh} {a.name_en}（{a.code}）
    </option>
  ));
}

interface RouteFormProps {
  subs: readonly RouteSubscription[];
  /** Present when editing an existing subscription. */
  editing?: RouteSubscription;
  submitLabel: string;
  onSubmit: (value: Validated) => void;
  onCancel?: () => void;
}

export function RouteForm({ subs, editing, submitLabel, onSubmit, onCancel }: RouteFormProps) {
  const id = useId();
  const [origin, setOrigin] = useState(editing?.origin ?? "");
  const [destination, setDestination] = useState(editing?.destination ?? "");
  const [targetText, setTargetText] = useState(editing ? String(editing.target_price) : "");
  const [errors, setErrors] = useState<ErrorCode[]>([]);

  function submit(e: FormEvent) {
    e.preventDefault();
    const result = validate(
      subs,
      { origin, destination, targetText },
      editing ? { editingRoute: editing.route } : {},
    );
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    setErrors([]);
    onSubmit(result.value);
    if (!editing) {
      setOrigin("");
      setDestination("");
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
      <div className="grid gap-4 sm:grid-cols-3">
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
            {options(ORIGINS)}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-destination`}>目的地</Label>
          <select
            id={`${id}-destination`}
            className={selectClass}
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
            aria-invalid={has(
              "destination_required",
              "unsupported_destination",
              "same_origin_destination",
            )}
          >
            <option value="">請選擇目的地</option>
            {options(DESTINATIONS)}
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

      {errors.length > 0 && (
        <ul
          role="alert"
          className="space-y-1 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm"
        >
          {errors.map((code) => (
            <li key={code}>{errorMessage(code, { origin, destination, policy: POLICY })}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2">
        <Button type="submit">{submitLabel}</Button>
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel}>
            取消
          </Button>
        )}
      </div>
    </form>
  );
}
