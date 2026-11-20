import { describe, expect, it } from "vitest";

import {
  assertHttpModeLoopback,
  isLoopbackHost,
  m14HttpAllowed,
  m14PrototypeEnabled,
} from "./prototype-flag";

describe("M1.4 prototype build flag (fail-closed)", () => {
  it.each([
    ["serve", undefined, true], // local `vite` dev server
    ["serve", "development", true], // `vercel dev`
    ["build", "preview", true], // Vercel preview deployment
    ["build", "production", false],
    ["serve", "production", false], // production never, even on a dev server
    ["build", undefined, false], // unknown: local `vite build`, other hosts
    ["build", "development", false],
    ["build", "staging", false], // custom Vercel environments
    ["build", "", false],
    ["build", "Preview", false], // exact match only
  ] as const)("command=%s VERCEL_ENV=%s → %s", (command, vercelEnv, expected) => {
    expect(m14PrototypeEnabled({ command, vercelEnv })).toBe(expected);
  });
});

describe("M1.4 local HTTP mode build switch (fail-closed)", () => {
  it.each([
    ["serve", undefined, true], // local `vite` dev server
    ["serve", "development", true], // `vercel dev`
    ["build", undefined, false], // any local `vite build`
    ["build", "preview", false], // Vercel preview: mock only
    ["build", "production", false],
    ["serve", "production", false],
    ["serve", "preview", false],
    ["build", "development", false],
  ] as const)("command=%s VERCEL_ENV=%s → %s", (command, vercelEnv, expected) => {
    expect(m14HttpAllowed({ command, vercelEnv })).toBe(expected);
  });

  it.each([
    [undefined, true],
    ["localhost", true],
    ["127.0.0.1", true],
    ["::1", true],
    ["[::1]", true],
    [true, false], // `vite --host`: every interface
    [false, false],
    ["0.0.0.0", false],
    ["::", false],
    ["192.168.1.20", false],
    ["my-laptop.local", false],
    ["localhost.evil.example", false],
  ] as const)("dev-server host %s is loopback: %s", (host, expected) => {
    expect(isLoopbackHost(host)).toBe(expected);
  });

  it("refuses HTTP mode on a network-exposed dev server, and only then", () => {
    expect(() => assertHttpModeLoopback("http", true)).toThrow(/loopback/);
    expect(() => assertHttpModeLoopback("http", "0.0.0.0")).toThrow(/loopback/);
    expect(() => assertHttpModeLoopback("http", "127.0.0.1")).not.toThrow();
    expect(() => assertHttpModeLoopback("http", undefined)).not.toThrow();
    expect(() => assertHttpModeLoopback(undefined, "0.0.0.0")).not.toThrow(); // mock mode
    expect(() => assertHttpModeLoopback("mock", true)).not.toThrow();
  });
});
