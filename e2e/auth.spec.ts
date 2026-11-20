// Signed-in page: the app's real route guard (appLoader -> supabase.auth.getUser()) decides.
// Sessions are fake (e2e/fixtures.ts); this checks the app's handling, not production Supabase.
import { APP, FAKE_TOKENS, expect, openApp, signIn, test } from "./fixtures";

test("an unauthenticated visitor is sent to sign-in and the API is never called", async ({
  page,
  net,
}) => {
  await page.goto(APP);
  await expect(page).toHaveURL(/\/signin$/);
  expect(net.api).toEqual([]);
});

test("a valid session reaches the page, which uses only the local harness", async ({
  page,
  context,
  net,
  harness,
}) => {
  await harness.reset(["fixed_jan"]);
  await signIn(context, "valid");
  await openApp(page, 1);
  // The real guard asked (fake) Supabase about the session ...
  expect(net.supabaseUserCalls).toContain(FAKE_TOKENS.valid);
  // ... and the API was called with the dev token only, never with the Supabase session.
  expect(net.api.map((c) => `${c.method} ${c.path}`)).toEqual(["GET /subscriptions"]);
  for (const c of net.api) {
    expect(c.auth).toBe("Bearer harness-token-user");
    expect(c.auth).not.toContain(FAKE_TOKENS.valid);
  }
  expect(await harness.count("GET /subscriptions")).toBe(1);
});

test("an expired session (refresh refused) is sent to sign-in", async ({ page, context, net }) => {
  await signIn(context, "expired");
  await page.goto(APP);
  await expect(page).toHaveURL(/\/signin$/);
  expect(net.api).toEqual([]);
});

test("a revoked session (Supabase answers 401) is sent to sign-in", async ({
  page,
  context,
  net,
}) => {
  await signIn(context, "revoked");
  await page.goto(APP);
  await expect(page).toHaveURL(/\/signin$/);
  expect(net.supabaseUserCalls).toContain(FAKE_TOKENS.revoked);
  expect(net.api).toEqual([]);
});
