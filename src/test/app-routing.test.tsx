import { matchRoutes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { M14_PROTOTYPE_PATH, routes } from "@/router";

// Match routes without running loaders or rendering: loaders may need network the
// test run lacks, and jsdom never loads the stylesheets.
const leaf = (path: string) => matchRoutes(routes, path)?.at(-1)?.route;

describe("App routing", () => {
  it("matches the landing page for /", () => {
    expect(leaf("/")?.index).toBe(true);
  });

  it.each(["/app", "/signin", "/signup"])("matches a page for %s", (path) => {
    expect(leaf(path)?.path).toBe(path.slice(1));
  });

  it("guards /app with a loader", () => {
    expect(leaf("/app")?.loader).toBeTypeOf("function");
  });

  it("falls back to the 404 page for unknown paths", () => {
    expect(leaf("/does-not-exist")?.path).toBe("*");
  });

  it("serves the M1.4 prototype on its own route, without a login loader", () => {
    const route = leaf(`/${M14_PROTOTYPE_PATH}`);
    expect(route?.path).toBe("prototype/subscriptions");
    expect(route?.loader).toBeUndefined();
  });
});

describe("M1.4 prototype in production builds", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("is not routable when VITE_M14_PROTOTYPE is false, and /app is unchanged", async () => {
    vi.stubEnv("VITE_M14_PROTOTYPE", "false");
    vi.resetModules();
    const prod = await import("@/router");
    const prodLeaf = (path: string) => matchRoutes(prod.routes, path)?.at(-1)?.route;
    expect(prod.M14_PROTOTYPE_ENABLED).toBe(false);
    expect(prodLeaf("/prototype/subscriptions")?.path).toBe("*");
    expect(prodLeaf("/app")?.loader).toBeTypeOf("function");
  });
});
