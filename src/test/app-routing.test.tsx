import { matchRoutes } from "react-router";
import { describe, expect, it } from "vitest";

import { routes } from "@/router";

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
});
