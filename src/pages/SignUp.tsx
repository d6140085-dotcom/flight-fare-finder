import { AuthForm } from "@/components/AuthForm";
import { usePageMeta } from "@/lib/page-meta";

export default function SignUp() {
  usePageMeta({
    title: "Sign up — Flight Price Notifier",
    description: "Create a Flight Price Notifier account and get emailed when fares drop.",
    tags: {
      "og:title": "Sign up — Flight Price Notifier",
      "og:description": "Create an account and get emailed when fares drop.",
      "og:type": "website",
      "twitter:card": "summary",
    },
  });
  return <AuthForm mode="signup" />;
}
