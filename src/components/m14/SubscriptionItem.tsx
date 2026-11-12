import { useState, type ReactNode } from "react";
import {
  CircleAlert,
  Clock,
  Info,
  Pause,
  Pencil,
  Play,
  Plane,
  RotateCcw,
  Trash2,
} from "lucide-react";
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
import { routeLabel, routeLabelEn } from "@/lib/m14/catalog";
import { checkedAtLabel, type FareDisplay } from "@/lib/m14/fare-status";
import type { TargetComparison } from "@/lib/m14/fare-view";
import { effectiveMonth, monthLabel } from "@/lib/m14/months";
import { statusOf, type RouteSubscription, type Validated } from "@/lib/m14/subscriptions";
import { cn } from "@/lib/utils";
import { RouteForm } from "./RouteForm";

const twd = new Intl.NumberFormat("zh-TW");

const STATUS = {
  active: { label: "追蹤中", variant: "default" },
  paused: { label: "已暫停", variant: "secondary" },
  expired: { label: "已過期", variant: "destructive" },
} as const;

/** Short value for the 參考票價 cell when no amount is shown. */
const FARE_WORD: Record<FareDisplay["kind"], string> = {
  ok: "暫不顯示",
  pending: "尚未查詢",
  no_data: "暫無資料",
  error: "查詢失敗",
  paused: "已暫停",
  expired: "已過期",
};

interface SubscriptionItemProps {
  sub: RouteSubscription;
  subs: readonly RouteSubscription[];
  now: Date;
  /** What the latest fare check says. */
  fare: FareDisplay;
  /** Reference amount to show (null: none, e.g. no data or no affiliate link). */
  price?: number | null;
  comparison?: TargetComparison | null;
  itinerary?: { depart: string; return: string } | null;
  /** Search links block (only for active rows with a shown reference fare). */
  links?: ReactNode;
  /** A request for this row is in flight: actions are disabled. */
  busy?: boolean;
  /** May return a promise; resolving to false keeps the edit form open. */
  onEdit: (value: Validated) => void | boolean | Promise<boolean>;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
}

const md = (iso: string) => `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;

function Fact({
  term,
  children,
  className,
}: {
  term: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-xs text-muted-foreground">{term}</dt>
      <dd className="mt-0.5 font-semibold">{children}</dd>
    </div>
  );
}

export function SubscriptionItem({
  sub,
  subs,
  now,
  fare,
  price = null,
  comparison = null,
  itinerary = null,
  links,
  busy = false,
  onEdit,
  onPause,
  onResume,
  onDelete,
}: SubscriptionItemProps) {
  const [editing, setEditing] = useState(false);
  const label = routeLabel(sub.origin, sub.destination);
  // Several subscriptions can share a route (one per month): the accessible name includes it.
  const monthName =
    sub.departure_month === null
      ? `每月滾動（${monthLabel(effectiveMonth(null, now))}）`
      : monthLabel(sub.departure_month);
  const status = statusOf(sub, now);
  const shown = fare.kind === "ok" && price !== null;
  const problem = fare.kind === "error" || fare.kind === "expired";

  return (
    <li
      aria-label={`${label}・${monthName}`}
      className="rounded-2xl border border-border bg-card p-4 transition-colors hover:border-primary/50 sm:p-6"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-lg font-semibold">
            <Plane className="h-5 w-5 shrink-0" aria-hidden="true" /> {label}
          </h3>
          <p className="truncate text-xs text-muted-foreground">
            {sub.origin} → {sub.destination} · {routeLabelEn(sub.origin, sub.destination)}
          </p>
        </div>
        <Badge variant={STATUS[status].variant} className="shrink-0 self-start whitespace-nowrap">
          {STATUS[status].label}
        </Badge>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
        <Fact term="出發月份">
          {sub.departure_month === null ? (
            <>
              {monthLabel(effectiveMonth(null, now))}
              <span className="block text-xs font-normal text-muted-foreground">每月滾動</span>
            </>
          ) : (
            monthLabel(sub.departure_month)
          )}
        </Fact>
        <Fact term="目標價">NT${twd.format(sub.target_price)}</Fact>
        <Fact term="參考票價（來源：Aviasales）">
          {shown ? (
            <span className="text-primary">約 NT${twd.format(price)}</span>
          ) : (
            <span
              className={cn("font-normal", problem ? "text-destructive" : "text-muted-foreground")}
            >
              {FARE_WORD[fare.kind]}
            </span>
          )}
        </Fact>
        <Fact term="與目標價相比">
          {shown && comparison ? (
            <span
              data-comparison={comparison.kind}
              className={comparison.kind === "above" ? "text-muted-foreground" : "text-primary"}
            >
              {comparison.text}
            </span>
          ) : (
            <span className="font-normal text-muted-foreground">—</span>
          )}
        </Fact>
      </dl>

      {shown ? (
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {itinerary && (
            <span>
              參考行程：去程 {md(itinerary.depart)}・回程 {md(itinerary.return)}（來回）
            </span>
          )}
          {fare.checkedAt && (
            <span className="inline-flex items-center gap-1">
              <Clock className="h-3.5 w-3.5" aria-hidden="true" />
              {checkedAtLabel(fare.checkedAt)}
            </span>
          )}
        </p>
      ) : (
        <div
          data-fare-kind={fare.kind}
          className={cn(
            "mt-2 flex items-start gap-1.5 text-sm",
            problem ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {problem ? (
            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          ) : (
            <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          )}
          <p>
            {fare.text}
            {fare.checkedAt && (
              <span className="block text-xs text-muted-foreground">
                {checkedAtLabel(fare.checkedAt)}
              </span>
            )}
          </p>
        </div>
      )}
      {status === "expired" && (
        <p className="mt-1 text-sm text-destructive">
          請選擇新的出發月份以重新啟用（仍佔用 1 筆訂閱額度）。
        </p>
      )}

      {links}

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
        <div className="mt-4 flex flex-wrap gap-2 border-t border-border pt-3">
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
