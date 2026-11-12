import { useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CircleAlert, Loader2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RouteForm } from "@/components/m14/RouteForm";
import { AFFILIATE_DISCLOSURE, SHORT_DISCLAIMER, SearchLinks } from "@/components/m14/SearchLinks";
import { SubscriptionItem } from "@/components/m14/SubscriptionItem";
import {
  ApiError,
  apiErrorMessage,
  type ListResponse,
  type PatchRequest,
  type SubscriptionsClient,
} from "@/lib/m14/api";
import { routeLabel } from "@/lib/m14/catalog";
import { fareView } from "@/lib/m14/fare-view";
import type { Validated } from "@/lib/m14/subscriptions";

interface SubscriptionsManagerProps {
  client: SubscriptionsClient;
  /** Separates cached data per signed-in user (Supabase user id), or "preview". */
  cacheKey: string;
  now?: () => Date;
  /**
   * Aviasales marker for the labelled affiliate link. Phase 3A passes a test value only in mock
   * mode; null means no affiliate link can be built, so reference amounts and links are hidden
   * (Travelpayouts condition 3, docs/m1.4-phase2b2-search-links.md §2.3).
   */
  marker: string | null;
}

export function SubscriptionsManager({
  client,
  cacheKey,
  now = () => new Date(),
  marker,
}: SubscriptionsManagerProps) {
  const queryClient = useQueryClient();
  const queryKey = ["m14", "subscriptions", client.kind, cacheKey];
  /** Public ID of the subscription with a request in flight (R-MM-1: never the route pair). */
  const [busyId, setBusyId] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);

  const list = useQuery<ListResponse, unknown>({
    queryKey,
    queryFn: () => client.list(),
    // One retry for server/network errors; 4xx (e.g. 401) are final.
    retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 1,
    retryDelay: 300,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey });

  function reportFailure(err: unknown, ctx: { origin?: string; destination?: string } = {}) {
    setWriteError(apiErrorMessage(err, { ...ctx, now: now() }));
    if (err instanceof ApiError && (err.code === "conflict" || err.code === "not_found")) {
      void refresh();
    }
  }

  const create = useMutation({
    mutationFn: (v: Validated) =>
      client.create({
        origin: v.origin,
        destination: v.destination,
        departure_month: v.departure_month ?? "",
        target_price: v.target,
      }),
  });
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: PatchRequest }) => client.update(id, patch),
  });
  const remove = useMutation({
    mutationFn: ({ id, ifUpdatedAt }: { id: string; ifUpdatedAt: string }) =>
      client.remove(id, ifUpdatedAt),
  });

  async function run<T>(
    id: string | null,
    action: () => Promise<T>,
    success: string,
    ctx: { origin?: string; destination?: string } = {},
  ): Promise<boolean> {
    setBusyId(id);
    setWriteError(null);
    try {
      await action();
      toast.success(success);
      await refresh();
      return true;
    } catch (err) {
      reportFailure(err, ctx);
      return false;
    } finally {
      setBusyId(null);
    }
  }

  if (list.isPending) {
    return (
      <section aria-labelledby="subs-heading" aria-busy="true" className="mt-8">
        <h2 id="subs-heading" className="mb-3 text-lg font-semibold">
          我的訂閱
        </h2>
        <p role="status" className="mb-3 flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          載入訂閱中…
        </p>
        <div className="space-y-4">
          <Skeleton className="h-28 w-full rounded-2xl" />
          <Skeleton className="h-28 w-full rounded-2xl" />
        </div>
      </section>
    );
  }

  if (list.isError) {
    const unauthorized = list.error instanceof ApiError && list.error.code === "unauthorized";
    return (
      <section aria-labelledby="subs-heading" className="mt-8">
        <h2 id="subs-heading" className="mb-3 text-lg font-semibold">
          我的訂閱
        </h2>
        <div role="alert" className="rounded-2xl border border-destructive/50 bg-card p-6">
          <p className="flex items-start gap-2">
            <CircleAlert className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
            {unauthorized ? "登入已過期，請重新登入。" : apiErrorMessage(list.error)}
          </p>
          {unauthorized ? (
            <Button asChild variant="outline" className="mt-4">
              <Link to="/signin">重新登入</Link>
            </Button>
          ) : (
            <Button variant="outline" className="mt-4" onClick={() => list.refetch()}>
              <RefreshCw className="h-4 w-4" /> 重試
            </Button>
          )}
        </div>
      </section>
    );
  }

  const data = list.data;
  const subs = data.subscriptions;
  const today = now();
  const full = data.limits.used >= data.limits.max;
  const demoMarker = client.kind === "mock" && marker !== null;

  return (
    <>
      <div className="animate-fade-up rounded-2xl border border-border bg-card p-6 sm:p-8">
        <h2 className="text-lg font-semibold">新增訂閱</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          選擇出發地、目的地與出發月份，設定目標價，參考票價達標時寄 email 通知你。
        </p>
        {full && (
          <p
            role="status"
            className="mt-4 rounded-md border border-border bg-muted p-3 text-sm"
            data-testid="limit-reached"
          >
            已達 {data.limits.max}{" "}
            筆訂閱上限（已暫停與已過期的訂閱也算在內）。請先刪除不需要的訂閱，才能新增。
          </p>
        )}
        <div className="mt-4">
          <RouteForm
            subs={subs}
            now={today}
            submitLabel="開始追蹤"
            busy={create.isPending}
            disabled={full}
            onSubmit={(value) =>
              run(
                null,
                () => create.mutateAsync(value),
                `已新增「${routeLabel(value.origin, value.destination)}」`,
                value,
              )
            }
          />
        </div>
      </div>

      <section aria-labelledby="subs-heading" className="mt-8">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="subs-heading" className="text-lg font-semibold">
            我的訂閱
          </h2>
          <p className="text-sm text-muted-foreground" aria-live="polite">
            我的航線訂閱：{data.limits.used} / {data.limits.max}
          </p>
        </div>

        <div
          aria-label="參考票價說明"
          role="note"
          className="mb-4 space-y-1 rounded-xl border border-border bg-card/60 p-3 text-xs text-muted-foreground"
        >
          <p>{SHORT_DISCLAIMER}</p>
          <p>{AFFILIATE_DISCLOSURE}</p>
          <p>
            同一航線可以分別追蹤不同的出發月份，每個月份各算 1
            筆訂閱。舊版「每月滾動」的訂閱若與某個月份的訂閱查詢同一個月，同一天最多寄出一封通知。
          </p>
          {demoMarker && (
            <p className="font-medium text-foreground">
              開發預覽：Aviasales
              連結使用測試用標記（marker=000000），不是正式聯盟連結，請勿用於正式訂購。
            </p>
          )}
        </div>

        {writeError && (
          <p
            role="alert"
            className="mb-3 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm"
          >
            {writeError}
          </p>
        )}

        {subs.length === 0 ? (
          <p className="rounded-2xl border border-dashed border-border p-6 text-center text-muted-foreground">
            還沒有訂閱，從上方新增第一條航線吧。
          </p>
        ) : (
          <ul className="space-y-4" aria-label="訂閱列表">
            {subs.map((sub) => {
              const view = fareView(sub, today, marker);
              const label = routeLabel(sub.origin, sub.destination);
              return (
                <SubscriptionItem
                  key={sub.id}
                  sub={sub}
                  subs={subs}
                  now={today}
                  fare={view.fare}
                  price={view.price}
                  comparison={view.comparison}
                  itinerary={view.itinerary}
                  links={view.links ? <SearchLinks links={view.links} /> : null}
                  busy={busyId === sub.id}
                  onEdit={(value) => {
                    const patch: PatchRequest = { if_updated_at: sub.updated_at };
                    if (value.origin !== sub.origin || value.destination !== sub.destination) {
                      patch.origin = value.origin;
                      patch.destination = value.destination;
                    }
                    if (value.departure_month !== sub.departure_month) {
                      patch.departure_month = value.departure_month;
                    }
                    if (value.target !== sub.target_price) patch.target_price = value.target;
                    if (Object.keys(patch).length === 1) return true; // nothing changed
                    return run(
                      sub.id,
                      () => update.mutateAsync({ id: sub.id, patch }),
                      `已更新「${routeLabel(value.origin, value.destination)}」`,
                      value,
                    );
                  }}
                  onPause={() =>
                    void run(
                      sub.id,
                      () =>
                        update.mutateAsync({
                          id: sub.id,
                          patch: { alerts_paused: true, if_updated_at: sub.updated_at },
                        }),
                      `已暫停「${label}」`,
                    )
                  }
                  onResume={() =>
                    void run(
                      sub.id,
                      () =>
                        update.mutateAsync({
                          id: sub.id,
                          patch: { alerts_paused: false, if_updated_at: sub.updated_at },
                        }),
                      `已恢復追蹤「${label}」`,
                    )
                  }
                  onDelete={() =>
                    void run(
                      sub.id,
                      () => remove.mutateAsync({ id: sub.id, ifUpdatedAt: sub.updated_at }),
                      `已刪除「${label}」`,
                    )
                  }
                />
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}
