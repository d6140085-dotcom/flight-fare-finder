import { useReducer } from "react";
import { Link } from "react-router";
import { FlaskConical } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SiteHeader } from "@/components/SiteHeader";
import { RouteForm } from "@/components/m14/RouteForm";
import { SubscriptionItem } from "@/components/m14/SubscriptionItem";
import { POLICY } from "@/lib/m14/catalog";
import {
  activeCount,
  demoSubscriptions,
  errorMessage,
  reducer,
  type Action,
  type State,
} from "@/lib/m14/subscriptions";
import { usePageMeta } from "@/lib/page-meta";

/**
 * M1.4 preview-only prototype. Local mock data and component state only: it never calls the
 * subscription API, Supabase or AWS, and it is not linked from the production site.
 */
export default function PrototypeSubscriptions() {
  usePageMeta({
    title: "訂閱管理原型（預覽）— Flight Price Notifier",
    description: "M1.4 自訂航線訂閱原型，僅供預覽。",
    tags: { robots: "noindex, nofollow" },
  });
  const [state, dispatch] = useReducer(
    (s: State, a: Action) => reducer(s, a),
    undefined,
    (): State => ({ subs: demoSubscriptions(), lastError: null }),
  );
  const now = () => new Date().toISOString();
  const active = activeCount(state.subs);

  return (
    <div className="flex min-h-screen flex-col bg-hero">
      <SiteHeader
        right={
          <Button asChild variant="outline">
            <Link to="/">回首頁</Link>
          </Button>
        }
      />
      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-12 sm:px-6 sm:py-16">
        <div
          role="note"
          className="mb-6 flex items-start gap-3 rounded-2xl border border-primary/50 bg-accent p-4 text-sm"
        >
          <FlaskConical className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <p>
            <span className="font-semibold">原型預覽（Prototype）</span>
            ：此頁使用示範資料，所有變更只存在這個分頁中，重新整理就會還原。不會建立、修改或刪除任何真實訂閱，也不會寄出通知。
          </p>
        </div>

        <div className="animate-fade-up rounded-2xl border border-border bg-card p-6 sm:p-8">
          <h1 className="text-2xl font-bold sm:text-3xl">自訂航線訂閱</h1>
          <p className="mt-2 text-muted-foreground">
            選擇出發地與目的地，設定目標價，票價達標時寄 email 通知你。
          </p>
          <div className="mt-6">
            <h2 className="mb-3 text-lg font-semibold">新增訂閱</h2>
            <RouteForm
              subs={state.subs}
              submitLabel="開始追蹤"
              onSubmit={(value) => dispatch({ type: "create", value, now: now() })}
            />
          </div>
        </div>

        <section aria-labelledby="subs-heading" className="mt-8">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="subs-heading" className="text-lg font-semibold">
              我的訂閱
            </h2>
            <p className="text-sm text-muted-foreground" aria-live="polite">
              追蹤中 {active} / {POLICY.maxActive} · 共 {state.subs.length} 筆
            </p>
          </div>

          {state.lastError && (
            <p
              role="alert"
              className="mb-3 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm"
            >
              {errorMessage(state.lastError.code)}
            </p>
          )}

          {state.subs.length === 0 ? (
            <p className="rounded-2xl border border-dashed border-border p-6 text-center text-muted-foreground">
              還沒有訂閱，從上方新增第一條航線吧。
            </p>
          ) : (
            <ul className="space-y-4">
              {state.subs.map((sub) => (
                <SubscriptionItem
                  key={sub.route}
                  sub={sub}
                  subs={state.subs}
                  onEdit={(value) =>
                    dispatch({ type: "edit", route: sub.route, value, now: now() })
                  }
                  onPause={() => dispatch({ type: "pause", route: sub.route, now: now() })}
                  onResume={() => dispatch({ type: "resume", route: sub.route, now: now() })}
                  onDelete={() => dispatch({ type: "delete", route: sub.route })}
                />
              ))}
            </ul>
          )}

          <div className="mt-6 text-right">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => dispatch({ type: "reset", subs: demoSubscriptions() })}
            >
              重設示範資料
            </Button>
          </div>
        </section>
      </main>
      <footer className="border-t border-border py-8 text-center text-sm text-muted-foreground">
        M1.4 原型 · 示範資料
      </footer>
    </div>
  );
}
