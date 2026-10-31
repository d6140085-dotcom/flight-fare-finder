import { redirect, useLoaderData, useNavigate } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { User } from "@supabase/supabase-js";
import { LogOut } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { SiteHeader } from "@/components/SiteHeader";
import { PlanCard } from "@/components/PlanCard";
import { PLANS, listSubscriptions, saveSubscription, type Subscription } from "@/lib/flight-api";
import { usePageMeta } from "@/lib/page-meta";

// Runs in the browser before /app renders (replaces TanStack Router's beforeLoad).
export async function appLoader() {
  const { data } = await supabase.auth.getUser();
  if (!data.user) throw redirect("/signin");
  return { user: data.user };
}

export default function AppPage() {
  usePageMeta({
    title: "Dashboard — Flight Price Notifier",
    description: "Your Flight Price Notifier dashboard.",
    tags: {
      "og:title": "Dashboard — Flight Price Notifier",
      "og:description": "Your fare-tracking dashboard.",
      "og:type": "website",
      "twitter:card": "summary",
      robots: "noindex",
    },
  });
  const { user } = useLoaderData() as { user: User };
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const subsQuery = useQuery({
    queryKey: ["subscriptions", user.id],
    queryFn: listSubscriptions,
    retry: 1,
  });
  const byRoute = new Map((subsQuery.data ?? []).map((s) => [s.route, s]));

  async function save(planName: (typeof PLANS)[number]["planName"], targetPrice: number) {
    const saved = await saveSubscription(planName, targetPrice);
    queryClient.setQueryData<Subscription[]>(["subscriptions", user.id], (old = []) => [
      ...old.filter((s) => s.route !== saved.route),
      saved,
    ]);
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
          <Button variant="outline" onClick={signOut}>
            <LogOut className="h-4 w-4" /> Sign out
          </Button>
        }
      />
      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-20 sm:px-6">
        <div className="animate-fade-up rounded-2xl border border-border bg-card p-8 sm:p-10">
          <h1 className="text-3xl font-bold break-all">Hi {user.email}</h1>
          <p className="mt-4 text-lg">選一條航線、設定目標價，票價達標時寄 email 通知你。</p>
          <p className="mt-2 text-muted-foreground">
            Pick a route and a target price. We email you when the fare reaches it.
          </p>
        </div>

        {subsQuery.isPending ? (
          <p className="mt-8 text-muted-foreground">載入中… Loading your subscriptions…</p>
        ) : subsQuery.isError ? (
          <div className="mt-8 rounded-2xl border border-destructive/50 bg-card p-6">
            <p>無法載入訂閱 Couldn't load your subscriptions: {subsQuery.error.message}</p>
            <Button variant="outline" className="mt-4" onClick={() => subsQuery.refetch()}>
              重試 Retry
            </Button>
          </div>
        ) : (
          <div className="mt-8 grid gap-6 sm:grid-cols-2">
            {PLANS.map((plan) => (
              <PlanCard
                key={plan.planName}
                plan={plan}
                subscription={byRoute.get(plan.route)}
                onSave={(target) => save(plan.planName, target)}
              />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
