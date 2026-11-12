import { matchRoutes, type RouteObject } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

// The prototype flag is read when @/router is first evaluated, so each case stubs the env and
// re-imports the module. Routes are matched without running loaders or rendering.
async function loadRouter(flag: string | undefined) {
  vi.resetModules();
  if (flag !== undefined) vi.stubEnv("VITE_M14_PROTOTYPE", flag);
  const mod = { ...(await import("@/router")), ...(await import("@/lib/m14/routes")) };
  const leaf = (path: string) => matchRoutes(mod.routes as RouteObject[], path)?.at(-1)?.route;
  return { ...mod, leaf };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("App routing", () => {
  it("matches the landing page, auth pages, guarded /app and the 404 fallback", async () => {
    const { leaf } = await loadRouter(undefined);
    expect(leaf("/")?.index).toBe(true);
    for (const path of ["/app", "/signin", "/signup"]) expect(leaf(path)?.path).toBe(path.slice(1));
    expect(leaf("/app")?.loader).toBeTypeOf("function");
    expect(leaf("/does-not-exist")?.path).toBe("*");
  });
});

describe("M1.4 prototype route (fail-closed)", () => {
  it.each([undefined, "", "false", "1", "TRUE", "yes"])(
    "is not routable when the build flag is %j, and /app is unchanged",
    async (flag) => {
      const r = await loadRouter(flag);
      expect(r.M14_PROTOTYPE_ENABLED).toBe(false);
      expect(r.leaf(`/${r.M14_PROTOTYPE_PATH}`)?.path).toBe("*");
      // Phase 3A signed-in mock page: also absent.
      expect(r.leaf(`/${r.M14_APP_PATH}`)?.path).toBe("*");
      expect(r.leaf("/app")?.loader).toBeTypeOf("function");
    },
  );

  it('is routable only when the build sets the flag to exactly "true", with no login loader', async () => {
    const r = await loadRouter("true");
    expect(r.M14_PROTOTYPE_ENABLED).toBe(true);
    const route = r.leaf(`/${r.M14_PROTOTYPE_PATH}`);
    expect(route?.path).toBe("prototype/subscriptions");
    expect(route?.loader).toBeUndefined();
    expect(r.leaf("/app")?.loader).toBeTypeOf("function");
  });

  it("guards the Phase 3A signed-in mock page with the same login loader as /app", async () => {
    const r = await loadRouter("true");
    const route = r.leaf(`/${r.M14_APP_PATH}`);
    expect(route?.path).toBe("app/subscriptions");
    expect(route?.loader).toBe(r.leaf("/app")?.loader);
  });
});
