import { useState, type ReactNode } from "react";
import { CircleAlert, Info, Pause, Pencil, Play, Plane, RotateCcw, Trash2 } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { placeLabel, routeLabel, routeLabelEn } from "@/lib/m14/catalog";
import { effectiveMonth, monthLabel } from "@/lib/m14/months";
import type { FareDisplay } from "@/lib/m14/fare-status";
import { statusOf, type RouteSubscription, type Validated } from "@/lib/m14/subscriptions";
import { cn } from "@/lib/utils";
import { RouteForm } from "./RouteForm";

const twd = new Intl.NumberFormat("zh-TW");

const STATUS = {
  active: { label: "追蹤中", variant: "default" },
  paused: { label: "已暫停", variant: "secondary" },
  expired: { label: "已過期", variant: "destructive" },
} as const;

interface SubscriptionItemProps {
  sub: RouteSubscription;
  subs: readonly RouteSubscription[];
  now: Date;
  /** What the latest fare check says (prototype: demo data). */
  fare: FareDisplay;
  /** Extra fare details under the fare line (last checked time, search links). */
  fareDetails?: ReactNode;
  /** A request for this row is in flight: actions are disabled. */
  busy?: boolean;
  /** May return a promise; resolving to false keeps the edit form open. */
  onEdit: (value: Validated) => void | boolean | Promise<boolean>;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
}

export function SubscriptionItem({
  sub,
  subs,
  now,
  fare,
  fareDetails,
  busy = false,
  onEdit,
  onPause,
  onResume,
  onDelete,
}: SubscriptionItemProps) {
  const [editing, setEditing] = useState(false);
  const label = routeLabel(sub.origin, sub.destination);
  const status = statusOf(sub, now);

  return (
    <li
      aria-label={label}
      className="rounded-2xl border border-border bg-card p-5 transition-colors hover:border-primary/50 sm:p-6"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-lg font-semibold">
            <Plane className="h-5 w-5 shrink-0" /> {label}
          </h3>
          <p className="text-sm text-muted-foreground">
            {placeLabel(sub.origin)} → {placeLabel(sub.destination)} ·{" "}
            {routeLabelEn(sub.origin, sub.destination)}
          </p>
          <p className="mt-2 text-sm">
            出發月份{" "}
            <span className="font-semibold">
              {sub.departure_month === null
                ? `每月滾動（目前查詢 ${monthLabel(effectiveMonth(null, now))}）`
                : monthLabel(sub.departure_month)}
            </span>
            {" · "}目標價 <span className="font-semibold">NT${twd.format(sub.target_price)}</span>
          </p>
          <p
            data-fare-kind={fare.kind}
            className={cn(
              "mt-2 flex items-start gap-1.5 text-sm",
              fare.kind === "error" || fare.kind === "expired"
                ? "text-destructive"
                : "text-muted-foreground",
            )}
          >
            {fare.kind === "error" || fare.kind === "expired" ? (
              <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            ) : (
              <Info className="mt-0.5 h-4 w-4 shrink-0" />
            )}
            <span>{fare.text}</span>
          </p>
          {fareDetails}
          {status === "expired" && (
            <p className="mt-1 text-sm text-destructive">
              請選擇新的出發月份以重新啟用（仍佔用 1 筆訂閱額度）。
            </p>
          )}
        </div>
        <Badge variant={STATUS[status].variant} className="shrink-0 self-start whitespace-nowrap">
          {STATUS[status].label}
        </Badge>
      </div>

      {editing ? (
        <div className="mt-4 border-t border-border pt-4">
          <RouteForm
            subs={subs}
            now={now}
            editing={sub}
            submitLabel="儲存變更"
            busy={busy}
            onSubmit={async (value) => {
              const ok = await onEdit(value);
              if (ok !== false) setEditing(false);
              return ok !== false;
            }}
            onCancel={() => setEditing(false)}
          />
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap gap-2">
          {status === "expired" ? (
            <Button size="sm" onClick={() => setEditing(true)} disabled={busy}>
              <RotateCcw className="h-4 w-4" /> 重新啟用
            </Button>
          ) : (
            <>
              <Button size="sm" variant="outline" onClick={() => setEditing(true)} disabled={busy}>
                <Pencil className="h-4 w-4" /> 編輯
              </Button>
              {sub.alerts_paused ? (
                <Button size="sm" variant="outline" onClick={onResume} disabled={busy}>
                  <Play className="h-4 w-4" /> 恢復追蹤
                </Button>
              ) : (
                <Button size="sm" variant="outline" onClick={onPause} disabled={busy}>
                  <Pause className="h-4 w-4" /> 暫停追蹤
                </Button>
              )}
            </>
          )}
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button size="sm" variant="outline" className="text-destructive" disabled={busy}>
                <Trash2 className="h-4 w-4" /> 刪除
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>刪除這筆訂閱？</AlertDialogTitle>
                <AlertDialogDescription>
                  確定要刪除「{label}」的降價追蹤嗎？刪除後不會再收到這條航線的通知，並釋出 1
                  筆訂閱額度。
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>取消</AlertDialogCancel>
                <AlertDialogAction onClick={onDelete}>確認刪除</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      )}
    </li>
  );
}
