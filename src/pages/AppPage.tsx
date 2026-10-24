import { redirect, useLoaderData, useNavigate } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import type { User } from "@supabase/supabase-js";
import { LogOut } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { SiteHeader } from "@/components/SiteHeader";
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
          <p className="mt-4 text-lg">你的航線追蹤儀表板即將上線 — 下一個里程碑會加上訂閱航線的功能。</p>
          <p className="mt-2 text-muted-foreground">Your dashboard is coming soon. Route-subscription will be added in the next milestone.</p>
        </div>
      </main>
    </div>
  );
}
