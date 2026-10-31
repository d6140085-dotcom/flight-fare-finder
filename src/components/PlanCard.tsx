import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Plane } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { parseTargetPrice, type PLANS, type Subscription } from "@/lib/flight-api";

const twd = new Intl.NumberFormat("zh-TW");

interface PlanCardProps {
  plan: (typeof PLANS)[number];
  subscription?: Subscription | undefined;
  onSave: (targetPrice: number) => Promise<void>;
}

export function PlanCard({ plan, subscription, onSave }: PlanCardProps) {
  const [value, setValue] = useState(subscription ? String(subscription.target_price) : "");
  const [saving, setSaving] = useState(false);
  const inputId = `target-${plan.planName}`;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const target = parseTargetPrice(value);
    if (target === null) {
      toast.error("請輸入 1 到 1,000,000 之間的整數 Enter a whole number from 1 to 1,000,000.");
      return;
    }
    setSaving(true);
    try {
      await onSave(target);
      toast.success(`${plan.label}：目標價 NT$${twd.format(target)} 已儲存 Saved.`);
    } catch (err) {
      toast.error(`儲存失敗 Save failed: ${err instanceof Error ? err.message : "unknown error"}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      className="flex h-full flex-col rounded-2xl border border-border bg-card p-6"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-xl font-semibold">
            <Plane className="h-5 w-5" /> {plan.label}
          </h2>
          <p className="text-sm text-muted-foreground">{plan.labelEn}</p>
        </div>
        {subscription && <Badge>已訂閱 Subscribed</Badge>}
      </div>

      {subscription ? (
        <p className="mt-4 text-sm">
          目前目標價 Current target:{" "}
          <span className="font-semibold">NT${twd.format(subscription.target_price)}</span>
        </p>
      ) : (
        <p className="mt-4 text-sm text-muted-foreground">
          近期最低價約 NT${twd.format(plan.hintTwd)}，可參考設定。Recent lowest fare is around NT$
          {twd.format(plan.hintTwd)}.
        </p>
      )}

      <div className="mt-4 space-y-2">
        <Label htmlFor={inputId}>目標價 Target price (TWD)</Label>
        <Input
          id={inputId}
          inputMode="numeric"
          placeholder={String(plan.hintTwd)}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </div>

      <Button type="submit" className="mt-4" disabled={saving}>
        {saving
          ? "儲存中… Saving…"
          : subscription
            ? "更新目標價 Update target"
            : "開始追蹤 Start tracking"}
      </Button>
    </form>
  );
}
