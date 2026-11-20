// Phase 3B-C: SubscriptionsManager against a stub API client (the HttpSubscriptionsClient
// interface): server data is used as given, and a failed write is never resent.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import vectors from "../../../aws/tests/vectors/subscriptions-http.json";
import { SubscriptionsManager } from "./SubscriptionsManager";
import {
  ApiError,
  type ApiErrorCode,
  type ListResponse,
  type SubscriptionDto,
  type SubscriptionsClient,
} from "@/lib/m14/api";
import { UNCERTAIN_MESSAGE } from "@/lib/m14/write-outcome";

vi.mock("@/integrations/supabase/client", () => ({
  supabase: { auth: { signOut: vi.fn(), getUser: vi.fn(), getSession: vi.fn() } },
}));

const NOW = new Date(vectors.now); // 2026-11-05 Taipei: selectable 2026-12 … 2027-11
const LIST = vectors.cases.find((c) => c.name === "list_mixed")!.response
  .body as unknown as ListResponse;
const clone = <T,>(v: T): T => structuredClone(v);

function stub(data: ListResponse = clone(LIST)) {
  const state = { data };
  const client = {
    kind: "http" as const,
    list: vi.fn(async () => clone(state.data)),
    create: vi.fn<SubscriptionsClient["create"]>(),
    update: vi.fn<SubscriptionsClient["update"]>(),
    remove: vi.fn<SubscriptionsClient["remove"]>(),
  };
  return { client, state };
}

function renderManager(client: SubscriptionsClient) {
  const qc = new QueryClient({ defaultQueries: undefined } as never);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SubscriptionsManager client={client} cacheKey="u1" now={() => NOW} marker={null} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

const list = () => screen.getByRole("list", { name: "訂閱列表" });
const card = (route: string) =>
  within(list()).getByRole("listitem", { name: new RegExp(`^${route}・`) });
const createForm = () => screen.getByRole("form", { name: "新增訂閱" });
const loaded = (used: number) => screen.findByText(`我的航線訂閱：${used} / 10`);
const fail = (code: ApiErrorCode, status: number) => new ApiError(code, status);

function fillCreate(dest = "LON", month = "2027-03", target = "12000") {
  const form = createForm();
  fireEvent.change(within(form).getByLabelText("出發地"), { target: { value: "TPE" } });
  const box = within(form).getByRole("combobox", { name: "目的地" });
  fireEvent.change(box, { target: { value: dest } });
  fireEvent.keyDown(box, { key: "Enter" });
  fireEvent.change(within(form).getByLabelText("出發月份"), { target: { value: month } });
  fireEvent.change(within(form).getByLabelText("目標價（新台幣）"), { target: { value: target } });
}

function dto(over: Partial<SubscriptionDto>): SubscriptionDto {
  const base = LIST.subscriptions.find((s) => s.id === "TPE-OSA_2027-01")!;
  return { ...clone(base), ...over };
}

describe("server data", () => {
  it("shows the server's count, statuses and nullable legacy timestamps", async () => {
    const { client } = stub();
    renderManager(client);
    await loaded(5);
    expect(within(list()).getAllByRole("listitem")).toHaveLength(5);
    expect(within(card("高雄 → 沖繩")).getAllByText("已暫停").length).toBeGreaterThan(0);
    expect(within(card("台中 → 首爾")).getAllByText("已過期").length).toBeGreaterThan(0);
    // The M1 row without created_at/updated_at renders like any other.
    expect(within(card("台北 → 首爾")).getAllByText("追蹤中").length).toBeGreaterThan(0);
  });

  it("uses the server's status even where a local calculation would differ", async () => {
    const data = clone(LIST);
    data.subscriptions = [dto({ status: "expired" })]; // 2027-01 is not expired by the local clock
    data.limits = { max: 10, used: 1 };
    renderManager(stub(data).client);
    await loaded(1);
    expect(within(card("台北 → 大阪")).getAllByText("已過期").length).toBeGreaterThan(0);
  });

  it("offers exactly the server's selectable months", async () => {
    const data = clone(LIST);
    data.months = { current: "2026-11", selectable: ["2027-02", "2027-05"] };
    renderManager(stub(data).client);
    await loaded(5);
    const options = within(within(createForm()).getByLabelText("出發月份"))
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    expect(options).toEqual(["2027-02", "2027-05"]);
  });

  it("accepts a server month the local calculation would not offer", async () => {
    const data = clone(LIST);
    data.months = { current: "2026-11", selectable: ["2028-01"] };
    const { client } = stub(data);
    client.create.mockResolvedValue(dto({ id: "TPE-LON_2028-01", departure_month: "2028-01" }));
    renderManager(client);
    await loaded(5);
    fillCreate("LON", "2028-01");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    await waitFor(() => expect(client.create).toHaveBeenCalledTimes(1));
    expect(client.create.mock.calls[0]![0]).toMatchObject({ departure_month: "2028-01" });
  });

  it("shows the limit from the server and disables creating at 10 of 10", async () => {
    const data = clone(LIST);
    data.limits = { max: 10, used: 10 };
    renderManager(stub(data).client);
    await screen.findByText("我的航線訂閱：10 / 10");
    expect(screen.getByTestId("limit-reached")).toHaveTextContent("已達 10");
    expect(within(createForm()).getByRole("button", { name: "開始追蹤" })).toBeDisabled();
  });
});

describe("failed writes are never resent", () => {
  it.each([
    ["conflict", 409],
    ["not_found", 404],
    ["version_required", 428],
  ] as const)("%s on pause: one write, one reload, message", async (code, status) => {
    const { client } = stub();
    client.update.mockRejectedValue(fail(code, status));
    renderManager(client);
    await loaded(5);
    fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "暫停追蹤" }));
    await screen.findByRole("alert");
    await waitFor(() => expect(client.list).toHaveBeenCalledTimes(2));
    expect(client.update).toHaveBeenCalledTimes(1);
    expect(client.update.mock.calls[0]![1].if_version).toBe(
      LIST.subscriptions.find((s) => s.id === "TPE-OSA_2027-01")!.version,
    );
  });

  it.each([
    ["conflict", 409],
    ["version_required", 428],
  ] as const)("%s on delete: one write, one reload", async (code, status) => {
    const { client } = stub();
    client.remove.mockRejectedValue(fail(code, status));
    renderManager(client);
    await loaded(5);
    fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "刪除" }));
    fireEvent.click(await screen.findByRole("button", { name: "確認刪除" }));
    await waitFor(() => expect(client.list).toHaveBeenCalledTimes(2));
    expect(client.remove).toHaveBeenCalledTimes(1);
  });

  const uncertain = [
    ["network_error", 0],
    ["service_unavailable", 503],
    ["invalid_response", 200],
    ["internal_error", 500],
  ] as const;

  it.each(uncertain)(
    "%s on create: uncertain message, one reload, input kept",
    async (code, status) => {
      const { client } = stub();
      client.create.mockRejectedValue(fail(code, status));
      renderManager(client);
      await loaded(5);
      fillCreate();
      fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
      expect(await screen.findByText(UNCERTAIN_MESSAGE)).toBeInTheDocument();
      await waitFor(() => expect(client.list).toHaveBeenCalledTimes(2));
      expect(client.create).toHaveBeenCalledTimes(1);
      expect(
        (within(createForm()).getByLabelText("目標價（新台幣）") as HTMLInputElement).value,
      ).toBe("12000");
      expect((within(createForm()).getByLabelText("出發月份") as HTMLSelectElement).value).toBe(
        "2027-03",
      );
    },
  );

  it.each(uncertain)(
    "%s on edit and delete: uncertain message, one reload, no resend",
    async (code, status) => {
      const { client } = stub();
      client.update.mockRejectedValue(fail(code, status));
      client.remove.mockRejectedValue(fail(code, status));
      renderManager(client);
      await loaded(5);
      fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "編輯" }));
      const edit = within(card("台北 → 大阪")).getByRole("form", { name: "編輯訂閱" });
      fireEvent.change(within(edit).getByLabelText("目標價（新台幣）"), {
        target: { value: "7000" },
      });
      fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
      expect(await screen.findByText(UNCERTAIN_MESSAGE)).toBeInTheDocument();
      await waitFor(() => expect(client.list).toHaveBeenCalledTimes(2));
      // The edit form stays open with what was typed.
      const still = within(card("台北 → 大阪")).getByRole("form", { name: "編輯訂閱" });
      expect((within(still).getByLabelText("目標價（新台幣）") as HTMLInputElement).value).toBe(
        "7000",
      );
      fireEvent.click(within(still).getByRole("button", { name: "取消" }));
      fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "刪除" }));
      fireEvent.click(await screen.findByRole("button", { name: "確認刪除" }));
      await waitFor(() => expect(client.list).toHaveBeenCalledTimes(3));
      expect(client.update).toHaveBeenCalledTimes(1);
      expect(client.remove).toHaveBeenCalledTimes(1);
    },
  );

  it("duplicate_subscription: message only, no reload, no resend", async () => {
    const { client } = stub();
    client.create.mockRejectedValue(
      new ApiError("duplicate_subscription", 409, {
        route: "TPE-LON",
        departure_month: "2027-03",
      }),
    );
    renderManager(client);
    await loaded(5);
    fillCreate();
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    await screen.findByRole("alert");
    await new Promise((r) => setTimeout(r, 30));
    expect(client.list).toHaveBeenCalledTimes(1);
    expect(client.create).toHaveBeenCalledTimes(1);
  });

  it("limit_reached: reloads the count once, no resend", async () => {
    const { client, state } = stub();
    client.create.mockImplementation(async () => {
      state.data = { ...clone(LIST), limits: { max: 10, used: 10 } };
      throw new ApiError("limit_reached", 409, { max: 10, used: 10 });
    });
    renderManager(client);
    await loaded(5);
    fillCreate();
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    await screen.findByText("我的航線訂閱：10 / 10");
    expect(client.list).toHaveBeenCalledTimes(2);
    expect(client.create).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["unauthorized", 401, /登入已過期/],
    ["too_many_requests", 429, /.+/],
  ] as const)("%s: message, no reload, no retry", async (code, status, text) => {
    const { client } = stub();
    client.update.mockRejectedValue(fail(code, status));
    renderManager(client);
    await loaded(5);
    fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "暫停追蹤" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    await new Promise((r) => setTimeout(r, 30));
    expect(client.list).toHaveBeenCalledTimes(1);
    expect(client.update).toHaveBeenCalledTimes(1);
  });
});

describe("successful writes", () => {
  it("a month move continues under the new subscription ID", async () => {
    const { client, state } = stub();
    const moved = dto({
      id: "TPE-OSA_2027-02",
      departure_month: "2027-02",
      version: "movedVersion0000000000",
    });
    client.update.mockImplementation(async () => {
      state.data = {
        ...clone(LIST),
        subscriptions: LIST.subscriptions.map((s) => (s.id === "TPE-OSA_2027-01" ? moved : s)),
      };
      return moved;
    });
    renderManager(client);
    await loaded(5);
    fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "編輯" }));
    const edit = within(card("台北 → 大阪")).getByRole("form", { name: "編輯訂閱" });
    fireEvent.change(within(edit).getByLabelText("出發月份"), { target: { value: "2027-02" } });
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    await waitFor(() =>
      expect(
        within(list()).getByRole("listitem", { name: "台北 → 大阪・2027 年 2 月" }),
      ).toBeInTheDocument(),
    );
    expect(
      within(list()).queryByRole("listitem", { name: "台北 → 大阪・2027 年 1 月" }),
    ).toBeNull();
    expect(screen.queryByRole("form", { name: "編輯訂閱" })).toBeNull();
    expect(client.update).toHaveBeenCalledWith("TPE-OSA_2027-01", {
      departure_month: "2027-02",
      if_version: LIST.subscriptions.find((s) => s.id === "TPE-OSA_2027-01")!.version,
    });
    // The next write on that card uses the new ID and version.
    client.update.mockResolvedValue({ ...moved, alerts_paused: true, status: "paused" });
    fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "暫停追蹤" }));
    await waitFor(() => expect(client.update).toHaveBeenCalledTimes(2));
    expect(client.update.mock.calls[1]).toEqual([
      "TPE-OSA_2027-02",
      { alerts_paused: true, if_version: "movedVersion0000000000" },
    ]);
  });

  it("allows one write at a time: double submissions are ignored and other actions disabled", async () => {
    const { client } = stub();
    let finish: (v: SubscriptionDto) => void = () => undefined;
    client.create.mockImplementation(() => new Promise((r) => (finish = r)));
    renderManager(client);
    await loaded(5);
    fillCreate();
    const submit = within(createForm()).getByRole("button", { name: "開始追蹤" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.submit(createForm());
    await waitFor(() => expect(client.create).toHaveBeenCalledTimes(1));
    expect(within(createForm()).getByRole("button", { name: "處理中…" })).toBeDisabled();
    for (const name of ["暫停追蹤", "編輯", "刪除"]) {
      expect(within(card("台北 → 大阪")).getByRole("button", { name })).toBeDisabled();
    }
    fireEvent.click(within(card("台北 → 大阪")).getByRole("button", { name: "暫停追蹤" }));
    expect(client.update).not.toHaveBeenCalled();
    finish(dto({ id: "TPE-LON_2027-03", departure_month: "2027-03" }));
    await waitFor(() =>
      expect(within(card("台北 → 大阪")).getByRole("button", { name: "暫停追蹤" })).toBeEnabled(),
    );
    expect(client.create).toHaveBeenCalledTimes(1);
  });
});
