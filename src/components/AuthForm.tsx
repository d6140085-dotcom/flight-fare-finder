import { Link, useNavigate } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SiteHeader } from "@/components/SiteHeader";

export function AuthForm({ mode }: { mode: "signin" | "signup" }) {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const isSignUp = mode === "signup";

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    const { data, error } = isSignUp
      ? await supabase.auth.signUp({ email, password, options: { emailRedirectTo: `${window.location.origin}/app` } })
      : await supabase.auth.signInWithPassword({ email, password });
    setLoading(false);
    if (error) return toast.error(error.message);
    if (!data.session) return toast.success("請到信箱確認帳號 Check your email to confirm.");
    navigate({ to: "/app" });
  }

  return (
    <div className="flex min-h-screen flex-col bg-hero">
      <SiteHeader />
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <form onSubmit={onSubmit} className="animate-fade-up w-full max-w-sm space-y-5 rounded-2xl border border-border bg-card p-8 shadow-glow">
          <div>
            <h1 className="text-2xl font-bold">{isSignUp ? "建立帳號 Sign up" : "登入 Sign in"}</h1>
            <p className="mt-1 text-sm text-muted-foreground">Flight Price Notifier</p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="password">Password</Label>
            <Input id="password" type="password" required minLength={6} value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <Button type="submit" className="w-full" disabled={loading}>
            {loading ? "..." : isSignUp ? "Sign up / 註冊" : "Sign in / 登入"}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            {isSignUp ? (
              <>已有帳號？ <Link to="/signin" className="text-primary hover:underline">Sign in</Link></>
            ) : (
              <>還沒有帳號？ <Link to="/signup" className="text-primary hover:underline">Sign up</Link></>
            )}
          </p>
        </form>
      </main>
    </div>
  );
}
