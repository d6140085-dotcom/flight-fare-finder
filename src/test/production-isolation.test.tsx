// @vitest-environment jsdom
// Phase 3A isolation: production builds must not reach the mock subscription pages, construct
// mock data or show the test affiliate marker — even by direct URL — and M1 must be unchanged.
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchRoutes, type RouteObject } from "react-router";
import { render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.resetModules();
});

/** Evaluate the app as a production build would: flag compiled to "false", MODE production. */
async function asProduction() {
  vi.resetModules();
  vi.stubEnv("VITE_M14_PROTOTYPE", "false");
  vi.stubEnv("MODE", "production");
  const router = await import("@/router");
  const routes = await import("@/lib/m14/routes");
  const leaf = (path: string) => matchRoutes(router.routes as RouteObject[], path)?.at(-1)?.route;
  return { router, routes, leaf };
}

describe("production builds: direct navigation", () => {
  it.each([
    "/app/subscriptions",
    "/app/subscriptions/",
    "/prototype/subscriptions",
    "/prototype/subscriptions/",
    "/prototype/subscriptions?scenario=demo",
  ])("%s falls through to the 404 route", async (path) => {
    const { leaf, routes } = await asProduction();
    expect(routes.M14_PROTOTYPE_ENABLED).toBe(false);
    expect(leaf(path.split("?")[0] as string)?.path).toBe("*");
  });

  it("keeps the M1 routes and the /app login guard unchanged", async () => {
    const { leaf } = await asProduction();
    expect(leaf("/")?.index).toBe(true);
    for (const p of ["/app", "/signin", "/signup"]) expect(leaf(p)?.path).toBe(p.slice(1));
    expect(leaf("/app")?.loader).toBeTypeOf("function");
  });
});

describe("production builds: no mock data and no silent fallback", () => {
  it("refuses to construct the mock client", async () => {
    await asProduction();
    const { MockSubscriptionsClient } = await import("@/lib/m14/mock-client");
    expect(() => new MockSubscriptionsClient()).toThrow(/not available/);
  });

  it("has no client to fall back to (throws instead of returning mock data)", async () => {
    await asProduction();
    const { createSubscriptionsClient, ClientUnavailable } =
      await import("@/lib/m14/client-factory");
    expect(() => createSubscriptionsClient()).toThrow(ClientUnavailable);
  });

  it("renders the 404 page, not mock data or the test marker, if the page is ever mounted", async () => {
    await asProduction();
    const { default: Page } = await import("@/pages/M14Subscriptions");
    render(
      <MemoryRouter>
        <Page mode="preview" latencyMs={0} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("404");
    expect(document.body.textContent).not.toMatch(/marker=000000|模擬|開發預覽/);
  });

  it("mockDataAllowed is true only for the preview flag or the test runner", async () => {
    const { mockDataAllowed } = await import("@/lib/m14/routes");
    expect(mockDataAllowed({ flag: "false", mode: "production" })).toBe(false);
    expect(mockDataAllowed({ flag: undefined, mode: "production" })).toBe(false);
    expect(mockDataAllowed({ flag: "TRUE", mode: "production" })).toBe(false);
    expect(mockDataAllowed({ flag: "true", mode: "production" })).toBe(true); // Vercel preview
    expect(mockDataAllowed({ flag: "false", mode: "test" })).toBe(true);
  });
});
