import { AuthForm } from "@/components/AuthForm";
import { usePageMeta } from "@/lib/page-meta";

export default function SignIn() {
  usePageMeta({
    title: "Sign in — Flight Price Notifier",
    description: "Sign in to Flight Price Notifier to manage your fare alerts.",
    tags: {
      "og:title": "Sign in — Flight Price Notifier",
      "og:description": "Sign in to manage your fare alerts.",
      "og:type": "website",
      "twitter:card": "summary",
    },
  });
  return <AuthForm mode="signin" />;
}
