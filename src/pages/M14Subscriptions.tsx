import { useState } from "react";
import { Link, useLoaderData, useNavigate } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import type { User } from "@supabase/supabase-js";
import { FlaskConical, LogOut } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { SiteHeader } from "@/components/SiteHeader";
import { SubscriptionsManager } from "@/components/m14/SubscriptionsManager";
import { createSubscriptionsClient } from "@/lib/m14/client-factory";
import { MOCK_SCENARIOS, type MockScenario } from "@/lib/m14/mock-client";
import { usePageMeta } from "@/lib/page-meta";
import { mockDataAllowed } from "@/lib/m14/routes";
import { NotFoundPage } from "./RootLayout";

/** Test-only Aviasales marker for mock mode. Never this project's partner ID. */
export const DEMO_MARKER = "000000";

interface M14SubscriptionsProps {
  /** "app": signed-in page (route guarded by appLoader). "preview": no login, demo user. */
  mode: "app" | "preview";
  now?: () => Date;
  /** Simulated latency of the mock API (ms); tests pass 0. */
  latencyMs?: number;
  initialScenario?: MockScenario;
}

/**
 * M1.4 Phase 3A: the subscription workflow against the in-memory mock API. Development and
 * Vercel preview builds only (the route is compiled in only when VITE_M14_PROTOTYPE is "true");
 * it never calls the subscriptions API, AWS or the fare check.
 */
/**
 * Defence in depth: the routes exist only in preview builds, and even if this page were reached
 * in another build it renders the 404 page and never creates mock data.
 */
export default function M14Subscriptions(props: M14SubscriptionsProps) {
  if (!mockDataAllowed()) return <NotFoundPage />;
  return <M14SubscriptionsPage {...props} />;
}

function M14SubscriptionsPage({
  mode,
  now = () => new Date(),
  latencyMs = 400,
  initialScenario = "demo",
}: M14SubscriptionsProps) {
  usePageMeta({
    title: "自訂航線訂閱（開發預覽）— Flight Price Notifier",
    description: "M1.4 自訂航線訂閱，使用模擬資料的開發預覽。",
    tags: { robots: "noindex, nofollow" },
  });
  const loader = useLoaderData() as { user: User } | undefined;
  const user = mode === "app" ? loader?.user : undefined;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [client] = useState(() =>
    createSubscriptionsClient({ now, latencyMs, scenario: initialScenario }),
  );
  const [scenario, setScenario] = useState<MockScenario>(initialScenario);

  function switchScenario(next: MockScenario) {
    client.setScenario(next);
    setScenario(next);
    void queryClient.resetQueries({ queryKey: ["m14", "subscriptions"] });
  }

  async function signOut() {
    await queryClient.cancelQueries();
    queryClient.clear();
    await supabase.auth.signOut();
    navigate("/signin", { replace: true });
  }

  return (
    <div className="flex min-h-screen flex-col bg-hero">
      <SiteHeader
        right={
          mode === "app" ? (
            <div className="flex gap-2">
              <Button asChild variant="ghost">
                <Link to="/app">返回</Link>
              </Button>
              <Button variant="outline" onClick={signOut}>
                <LogOut className="h-4 w-4" /> Sign out
              </Button>
            </div>
          ) : (
            <Button asChild variant="outline">
              <Link to="/">回首頁</Link>
            </Button>
          )
        }
      />
      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-10 sm:px-6 sm:py-16">
        <div
          role="note"
          aria-label="開發預覽"
          className="mb-6 flex items-start gap-3 rounded-2xl border border-primary/50 bg-accent p-4 text-sm"
        >
          <FlaskConical className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div className="min-w-0 space-y-2">
            <p>
              <span className="font-semibold">開發預覽（模擬資料）</span>
              ：此頁連接的是瀏覽器內的模擬
              API，不會建立、修改或刪除任何真實訂閱，也不會查詢票價或寄出通知。重新整理就會還原。
            </p>
            <label className="flex flex-wrap items-center gap-2">
              <span>模擬情境</span>
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={scenario}
                onChange={(e) => switchScenario(e.target.value as MockScenario)}
              >
                {MOCK_SCENARIOS.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>

        <h1 className="mb-6 text-2xl font-bold break-all sm:text-3xl">
          自訂航線訂閱
          {user?.email && (
            <span className="mt-1 block text-base font-normal text-muted-foreground">
              {user.email}
            </span>
          )}
        </h1>

        <SubscriptionsManager
          client={client}
          cacheKey={user?.id ?? "preview"}
          now={now}
          marker={DEMO_MARKER}
        />
      </main>
      <footer className="border-t border-border py-8 text-center text-sm text-muted-foreground">
        M1.4 Phase 3A · 模擬資料
      </footer>
    </div>
  );
}
