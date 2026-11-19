// @vitest-environment node
// Phase 3B-B: HttpSubscriptionsClient over real HTTP against the localhost-only harness
// (aws/tools/local_api.py -> real Lambda handler -> local DynamoDB; DynamoDB Local when
// DDB_ENDPOINT is set, otherwise moto). Opt-in, because it starts a Python process:
//   npm run test:harness            (needs python3 with boto3, moto, cryptography)
//   DDB_ENDPOINT=http://127.0.0.1:8000 npm run test:harness
// Node's fetch does not enforce browser CORS; CORS is checked as HTTP headers by the Python
// harness tests, and in a real browser by Playwright in Phase 3B-D.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { join } from "node:path";
import vectors from "../../../aws/tests/vectors/subscriptions-http.json";
import { ApiError, type CreateRequest, type PatchRequest } from "./api";
import { HttpSubscriptionsClient } from "./http-client";

const ENABLED = process.env["M14_HARNESS"] === "1";
const ROOT = join(__dirname, "..", "..", "..");

interface Info {
  api_url: string;
  control_url: string;
  control_token: string;
  engine: "dynamodb-local" | "moto";
}
interface Row extends Record<string, unknown> {
  route: string;
  email: string;
}
interface State {
  rows: Record<string, Row[]>;
  quota: Record<string, { count: number }>;
  requests: Record<string, number>;
}
interface Case {
  name: string;
  seed?: string[];
  seed_other?: string[];
  new_tokens?: string[];
  client: {
    op: "list" | "create" | "update" | "remove";
    id?: string;
    request?: CreateRequest;
    patch?: PatchRequest;
    if_version?: string | null;
  };
  response: { status: number; body: Record<string, unknown> | null };
  stored_keys_after: string[];
}

const cases = vectors.cases as unknown as Case[];
const rows = vectors.rows as unknown as Record<string, Record<string, unknown>>;

describe.skipIf(!ENABLED)("HttpSubscriptionsClient over real HTTP (local harness)", () => {
  let proc: ChildProcessWithoutNullStreams;
  let info: Info;

  beforeAll(async () => {
    // A clean environment: no proxies, no real credentials; the harness refuses anything else.
    const env: Record<string, string> = {
      PATH: process.env["PATH"] ?? "/usr/bin:/bin",
      HOME: process.env["HOME"] ?? "/tmp",
      LANG: "C.UTF-8",
    };
    if (process.env["DDB_ENDPOINT"]) env["DDB_ENDPOINT"] = process.env["DDB_ENDPOINT"];
    proc = spawn("python3", [join(ROOT, "aws/tools/local_api.py"), "serve", "--control"], {
      cwd: ROOT,
      env,
    });
    let stderr = "";
    proc.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    const line = await new Promise<string>((resolve, reject) => {
      const rl = createInterface({ input: proc.stdout });
      rl.once("line", resolve);
      proc.once("exit", (code) => reject(new Error(`harness exited (${code}): ${stderr}`)));
    });
    info = JSON.parse(line) as Info;
    console.info(`[local harness engine: ${info.engine}]`);
  }, 60_000);

  afterAll(() => {
    proc?.kill();
  });

  async function control<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${info.control_url}${path}`, {
      method,
      headers: { "X-Harness-Control": info.control_token, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    expect(res.status).toBe(200);
    return (await res.json()) as T;
  }
  const reset = (body: Record<string, unknown> = {}) =>
    control("POST", "/reset", { rows: {}, ...body });
  const state = () => control<State>("GET", "/state");
  const client = (token = vectors.token) =>
    new HttpSubscriptionsClient({ baseUrl: info.api_url, getToken: async () => token });

  function run(c: HttpSubscriptionsClient, k: Case): Promise<unknown> {
    switch (k.client.op) {
      case "list":
        return c.list();
      case "create":
        return c.create(k.client.request as CreateRequest);
      case "update":
        return c.update(k.client.id as string, k.client.patch as PatchRequest);
      case "remove":
        return c.remove(k.client.id as string, k.client.if_version as string);
    }
  }

  it.each(cases.map((k) => [k.name, k] as const))(
    "shared example %s",
    async (_name, k) => {
      const seeded = {
        [vectors.user]: (k.seed ?? []).map((n) => rows[n]),
        [vectors.other_user]: (k.seed_other ?? []).map((n) => rows[n]),
      };
      await reset({ now: vectors.now, new_tokens: k.new_tokens ?? [], rows: seeded });
      const outcome = await run(client(), k).then(
        (value) => ({ ok: true as const, value }),
        (error: unknown) => ({ ok: false as const, error }),
      );
      const { status, body } = k.response;
      if (status >= 400) {
        expect(outcome.ok).toBe(false);
        const err = (outcome as { error: ApiError }).error;
        expect(err).toBeInstanceOf(ApiError);
        expect([err.status, err.code]).toEqual([status, body?.["error"]]);
        expect(err.details).toEqual(body?.["details"] ?? {});
      } else {
        expect(outcome).toEqual({ ok: true, value: k.client.op === "remove" ? undefined : body });
      }
      const st = await state();
      const mine = (st.rows[vectors.user] ?? []).map((r) => r.route).sort();
      expect(mine).toEqual([...k.stored_keys_after].sort());
      expect(st.rows[vectors.other_user] ?? []).toEqual(
        seeded[vectors.other_user]!.map((r) => ({ ...r, email: vectors.other_user })),
      );
      expect(Object.values(st.requests).reduce((a, b) => a + b, 0)).toBe(1); // no retry
    },
    30_000,
  );

  it("chains versions through a real multi-month lifecycle", async () => {
    await reset();
    const c = client("harness-token-user");
    const jan = await c.create({
      origin: "TPE",
      destination: "TYO",
      departure_month: "2027-01",
      target_price: 9000,
    });
    const feb = await c.create({
      origin: "TPE",
      destination: "TYO",
      departure_month: "2027-02",
      target_price: 9000,
    });
    expect([jan.id, feb.id]).toEqual(["TPE-TYO_2027-01", "TPE-TYO_2027-02"]);
    const paused = await c.update(feb.id, { alerts_paused: true, if_version: feb.version });
    expect(paused.status).toBe("paused");
    // The stale version is refused once and not retried; the page would reload.
    const before = await state();
    const err = await c
      .update(feb.id, { alerts_paused: false, if_version: feb.version })
      .catch((e: unknown) => e);
    expect(err).toMatchObject({ code: "conflict", status: 409 });
    const after = await state();
    expect(after.requests["PATCH /subscriptions/{id}"]).toBe(
      (before.requests["PATCH /subscriptions/{id}"] ?? 0) + 1,
    );
    expect(after.rows["user@example.com"]).toEqual(before.rows["user@example.com"]);
    const moved = await c.update(jan.id, { departure_month: "2027-03", if_version: jan.version });
    expect(moved.id).toBe("TPE-TYO_2027-03");
    await c.remove(paused.id, paused.version);
    const list = await c.list();
    expect(list.subscriptions.map((s) => s.id)).toEqual(["TPE-TYO_2027-03"]);
    expect(list.limits).toEqual({ max: 10, used: 1 });
    expect((await state()).quota["user@example.com"]?.count).toBe(1);
  });

  it("maps real gateway faults and authentication failures", async () => {
    await reset();
    for (const [status, code] of [
      [429, "too_many_requests"],
      [502, "service_unavailable"],
      [503, "service_unavailable"],
      [504, "service_unavailable"],
    ] as const) {
      await control("POST", "/fault", { status });
      await expect(client("harness-token-user").list()).rejects.toMatchObject({ code, status });
    }
    for (const token of ["harness-token-expired", "unknown-token"]) {
      await expect(client(token).list()).rejects.toMatchObject({
        code: "unauthorized",
        status: 401,
      });
    }
    const other = await client("harness-token-other").list();
    expect(other.subscriptions).toEqual([]);
  });

  it("reports a closed port as network_error", async () => {
    const closed = new HttpSubscriptionsClient({
      baseUrl: "http://127.0.0.1:9",
      getToken: async () => "harness-token-user",
    });
    await expect(closed.list()).rejects.toMatchObject({ code: "network_error", status: 0 });
  });
});
