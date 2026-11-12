import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router";

import M14Subscriptions from "./M14Subscriptions";
import { SubscriptionsManager } from "@/components/m14/SubscriptionsManager";
import { MockSubscriptionsClient, type MockScenario } from "@/lib/m14/mock-client";

// Supabase is mocked: these tests never reach the network or a real session.
const { signOut } = vi.hoisted(() => ({ signOut: vi.fn(() => Promise.resolve({ error: null })) }));
vi.mock("@/integrations/supabase/client", () => ({
  supabase: { auth: { signOut, getUser: vi.fn(), getSession: vi.fn() } },
}));

// 2026-11-11 10:00 Taipei: selectable months 2026-12 … 2027-11; rolling rows check 2026-12.
const NOW = new Date("2026-11-11T02:00:00Z");
const now = () => NOW;
const TEST_USER = { id: "00000000-0000-0000-0000-000000000001", email: "user@example.com" };

function renderPage(
  opts: { mode?: "app" | "preview"; scenario?: MockScenario; loaderUser?: boolean } = {},
) {
  const qc = new QueryClient();
  const mode = opts.mode ?? "preview";
  const router = createMemoryRouter(
    [
      {
        path: "/x",
        element: (
          <M14Subscriptions
            mode={mode}
            now={now}
            latencyMs={0}
            initialScenario={opts.scenario ?? "demo"}
          />
        ),
        ...(mode === "app" ? { loader: () => ({ user: TEST_USER }) } : {}),
      },
      { path: "/signin", element: <p>sign-in page</p> },
      { path: "/app", element: <p>app page</p> },
    ],
    { initialEntries: ["/x"] },
  );
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
    router,
  };
}

const list = () => screen.getByRole("list", { name: "訂閱列表" });
const item = (name: string) => within(list()).getByRole("listitem", { name });
const createForm = () => screen.getByRole("form", { name: "新增訂閱" });
/** The value shown for one labelled fact on a card (出發月份, 目標價, 參考票價…, 與目標價相比). */
function fact(row: HTMLElement, term: string): string {
  const dt = within(row)
    .getAllByText(term)
    .find((el) => el.tagName === "DT");
  return dt?.nextElementSibling?.textContent ?? "";
}
const loaded = () => screen.findByText(/我的航線訂閱：\d+ \/ 10/);

function pickDestination(form: HTMLElement, query: string) {
  const box = within(form).getByRole("combobox", { name: "目的地" });
  fireEvent.change(box, { target: { value: query } });
  fireEvent.keyDown(box, { key: "Enter" });
}

function fill(
  form: HTMLElement,
  origin: string,
  destination: string,
  target: string,
  month?: string,
) {
  fireEvent.change(within(form).getByLabelText("出發地"), { target: { value: origin } });
  pickDestination(form, destination);
  if (month)
    fireEvent.change(within(form).getByLabelText("出發月份"), { target: { value: month } });
  fireEvent.change(within(form).getByLabelText("目標價（新台幣）"), { target: { value: target } });
}

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn(() => Promise.reject(new Error("network disabled in Phase 3A tests")));
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // Phase 3A never calls the subscriptions API, AWS or Supabase data endpoints.
  expect(fetchSpy).not.toHaveBeenCalled();
  // Mock data lives in memory only: nothing is persisted in browser storage.
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

describe("loading, empty and error states", () => {
  it("shows a loading state, then the list", async () => {
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("載入訂閱中");
    expect(await loaded()).toHaveTextContent("6 / 10");
  });

  it("shows an empty state", async () => {
    renderPage({ scenario: "empty" });
    expect(await screen.findByText("還沒有訂閱，從上方新增第一條航線吧。")).toBeInTheDocument();
    expect(screen.getByText("我的航線訂閱：0 / 10")).toBeInTheDocument();
  });

  it("shows a retryable error when the list can't be loaded", async () => {
    renderPage({ scenario: "list_error" });
    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert).toHaveTextContent("訂閱服務暫時發生錯誤");
    expect(within(alert).getByRole("button", { name: "重試" })).toBeInTheDocument();
  });

  it("asks to sign in again on 401 without retrying", async () => {
    renderPage({ scenario: "unauthorized" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("登入已過期");
    expect(within(alert).getByRole("link", { name: "重新登入" })).toHaveAttribute(
      "href",
      "/signin",
    );
  });

  it("switches mock scenarios from the development banner", async () => {
    renderPage();
    await loaded();
    fireEvent.change(screen.getByLabelText("模擬情境"), { target: { value: "empty" } });
    expect(await screen.findByText("我的航線訂閱：0 / 10")).toBeInTheDocument();
  });
});

describe("origin, destination and month selection", () => {
  it("offers exactly the three Taiwan origins and a searchable 95-destination picker", async () => {
    renderPage();
    await loaded();
    const origin = within(createForm()).getByLabelText("出發地");
    const codes = within(origin)
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    expect(codes).toEqual(["", "TPE", "KHH", "RMQ"]);
    const box = within(createForm()).getByRole("combobox", { name: "目的地" });
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "" } });
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(screen.getAllByRole("option", { hidden: false }).length).toBeGreaterThanOrEqual(95);
  });

  it("offers the next 12 months, defaulting to next month", async () => {
    renderPage();
    await loaded();
    const month = within(createForm()).getByLabelText("出發月份") as HTMLSelectElement;
    const values = Array.from(month.options).map((o) => o.value);
    expect(values).toHaveLength(12);
    expect(values[0]).toBe("2026-12");
    expect(values.at(-1)).toBe("2027-11");
    expect(month.value).toBe("2026-12");
  });

  it("creates a subscription for any origin, destination and chosen month", async () => {
    renderPage();
    await loaded();
    fill(createForm(), "RMQ", "OKA", "3500", "2027-04");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    expect(await screen.findByText("我的航線訂閱：7 / 10")).toBeInTheDocument();
    const row = item("台中 → 沖繩");
    expect(row).toHaveTextContent("2027 年 4 月");
    expect(row).toHaveTextContent("NT$3,500");
    expect(row).toHaveTextContent("追蹤中");
    // A new row has not been checked yet: no fare, no links.
    expect(row).toHaveTextContent("尚未查詢 2027 年 4 月 的票價");
    expect(within(row).queryAllByRole("link")).toHaveLength(0);
  });

  it("shows field errors and server-side duplicate errors", async () => {
    renderPage();
    await loaded();
    fill(createForm(), "TPE", "TYO", "0");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    const alert = await within(createForm()).findByRole("alert");
    expect(alert).toHaveTextContent("你已經訂閱過「台北 → 東京」");
    expect(alert).toHaveTextContent("目標價請輸入");
  });
});

describe("legacy rolling subscriptions", () => {
  it("shows the M1 row as rolling and keeps it rolling on a target-only edit", async () => {
    renderPage();
    await loaded();
    expect(fact(item("台北 → 東京"), "出發月份")).toBe("2026 年 12 月每月滾動");
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    const form = within(item("台北 → 東京")).getByRole("form", { name: "編輯訂閱" });
    expect((within(form).getByLabelText("出發月份") as HTMLSelectElement).value).toBe("rolling");
    fireEvent.change(within(form).getByLabelText("目標價（新台幣）"), {
      target: { value: "8500" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "儲存變更" }));
    await waitFor(() => expect(fact(item("台北 → 東京"), "目標價")).toBe("NT$8,500"));
    expect(fact(item("台北 → 東京"), "出發月份")).toContain("每月滾動");
  });

  it("moves the legacy row to a fixed month, after which rolling is not offered", async () => {
    renderPage();
    await loaded();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    const form = within(item("台北 → 東京")).getByRole("form", { name: "編輯訂閱" });
    fireEvent.change(within(form).getByLabelText("出發月份"), { target: { value: "2027-03" } });
    fireEvent.click(within(form).getByRole("button", { name: "儲存變更" }));
    await waitFor(() => expect(fact(item("台北 → 東京"), "出發月份")).toBe("2027 年 3 月"));
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    const again = within(item("台北 → 東京")).getByRole("form", { name: "編輯訂閱" });
    const values = Array.from(
      (within(again).getByLabelText("出發月份") as HTMLSelectElement).options,
    ).map((o) => o.value);
    expect(values).not.toContain("rolling");
  });
});

describe("limit of 10 subscriptions", () => {
  it("disables creating at the limit and explains that paused and expired rows count", async () => {
    renderPage({ scenario: "full" });
    await loaded();
    expect(screen.getByText("我的航線訂閱：10 / 10")).toBeInTheDocument();
    expect(screen.getByTestId("limit-reached")).toHaveTextContent("已暫停與已過期的訂閱也算在內");
    expect(within(createForm()).getByRole("button", { name: "開始追蹤" })).toBeDisabled();
  });

  it("frees a slot only when a subscription is deleted", async () => {
    renderPage({ scenario: "full" });
    await loaded();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "暫停追蹤" }));
    await waitFor(() => expect(item("台北 → 東京")).toHaveTextContent("已暫停"));
    expect(screen.getByText("我的航線訂閱：10 / 10")).toBeInTheDocument();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "刪除" }));
    fireEvent.click(await screen.findByRole("button", { name: "確認刪除" }));
    expect(await screen.findByText("我的航線訂閱：9 / 10")).toBeInTheDocument();
    expect(within(createForm()).getByRole("button", { name: "開始追蹤" })).toBeEnabled();
  });
});

describe("active, paused and expired states", () => {
  it("pauses and resumes; paused rows show no fare and no links", async () => {
    renderPage();
    await loaded();
    const paused = item("高雄 → 沖繩");
    expect(paused).toHaveTextContent("已暫停");
    expect(paused).toHaveTextContent("已暫停：暫時不查詢票價，也不寄送通知。");
    expect(within(paused).queryAllByRole("link")).toHaveLength(0);
    fireEvent.click(within(paused).getByRole("button", { name: "恢復追蹤" }));
    await waitFor(() => expect(item("高雄 → 沖繩")).toHaveTextContent("追蹤中"));

    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "暫停追蹤" }));
    await waitFor(() => expect(item("台北 → 東京")).toHaveTextContent("已暫停"));
    expect(item("台北 → 東京")).not.toHaveTextContent("NT$8,062");
    expect(within(item("台北 → 東京")).queryAllByRole("link")).toHaveLength(0);
  });

  it("reactivates an expired subscription by choosing a future month", async () => {
    renderPage();
    await loaded();
    expect(item("台中 → 首爾")).toHaveTextContent("已過期");
    fireEvent.click(within(item("台中 → 首爾")).getByRole("button", { name: "重新啟用" }));
    const form = within(item("台中 → 首爾")).getByRole("form", { name: "編輯訂閱" });
    fireEvent.change(within(form).getByLabelText("出發月份"), { target: { value: "2027-02" } });
    fireEvent.click(within(form).getByRole("button", { name: "儲存變更" }));
    await waitFor(() => expect(item("台中 → 首爾")).toHaveTextContent("追蹤中"));
  });
});

describe("editing and deleting", () => {
  it("changes the route and rejects a change onto an existing route", async () => {
    renderPage();
    await loaded();
    fireEvent.click(within(item("台北 → 倫敦")).getByRole("button", { name: "編輯" }));
    let form = within(item("台北 → 倫敦")).getByRole("form", { name: "編輯訂閱" });
    pickDestination(form, "SYD");
    fireEvent.click(within(form).getByRole("button", { name: "儲存變更" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("你已經訂閱過「台北 → 雪梨」");
    form = within(item("台北 → 倫敦")).getByRole("form", { name: "編輯訂閱" });
    pickDestination(form, "PAR");
    fireEvent.click(within(form).getByRole("button", { name: "儲存變更" }));
    expect(await screen.findByRole("listitem", { name: "台北 → 巴黎" })).toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "台北 → 倫敦" })).toBeNull();
    expect(screen.getByText("我的航線訂閱：6 / 10")).toBeInTheDocument();
  });

  it("deletes only after confirmation", async () => {
    renderPage();
    await loaded();
    fireEvent.click(within(item("台北 → 雪梨")).getByRole("button", { name: "刪除" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消" }));
    expect(item("台北 → 雪梨")).toBeInTheDocument();
    fireEvent.click(within(item("台北 → 雪梨")).getByRole("button", { name: "刪除" }));
    fireEvent.click(await screen.findByRole("button", { name: "確認刪除" }));
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "台北 → 雪梨" })).toBeNull());
    expect(screen.getByText("我的航線訂閱：5 / 10")).toBeInTheDocument();
  });
});

describe("fare presentation (mock fare checks)", () => {
  it("shows route, month, target, reference fare and status as the card's primary facts", async () => {
    renderPage();
    await loaded();
    const row = item("台北 → 東京");
    expect(within(row).getByRole("heading", { level: 3 })).toHaveTextContent("台北 → 東京");
    expect(fact(row, "目標價")).toBe("NT$9,000");
    expect(fact(row, "參考票價（來源：Aviasales）")).toBe("約 NT$8,062");
    expect(fact(row, "與目標價相比")).toBe("低於目標價 NT$938");
    expect(row).toHaveTextContent("追蹤中");
    expect(row).toHaveTextContent("最後查詢：2026/11/11 10:30（台灣時間）");
    expect(row).toHaveTextContent("參考行程：去程 12/11・回程 12/18（來回）");
    expect(row).not.toHaveTextContent("立即訂購");
    // The comparison describes the fare only; it never claims an email was sent.
    expect(row).not.toHaveTextContent(/已通知|已寄出|已發送|通知已/);
  });

  it("compares the reference fare with the target in both directions", async () => {
    renderPage();
    await loaded();
    // KHH-BKK: target NT$7,000, mock fare NT$6,480.
    expect(fact(item("高雄 → 曼谷"), "與目標價相比")).toBe("低於目標價 NT$520");
    fireEvent.click(within(item("高雄 → 曼谷")).getByRole("button", { name: "編輯" }));
    const form = within(item("高雄 → 曼谷")).getByRole("form", { name: "編輯訂閱" });
    fireEvent.change(within(form).getByLabelText("目標價（新台幣）"), {
      target: { value: "6000" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "儲存變更" }));
    await waitFor(() =>
      expect(fact(item("高雄 → 曼谷"), "與目標價相比")).toBe("高於目標價 NT$480"),
    );
  });

  it("keeps the required notices: source, approximate wording, disclaimer and affiliate disclosure", async () => {
    renderPage();
    await loaded();
    const notes = screen.getByRole("note", { name: "參考票價說明" });
    expect(notes).toHaveTextContent("參考票價來自 Aviasales（經 Travelpayouts 取得）");
    expect(notes).toHaveTextContent("金額為約略值");
    expect(notes).toHaveTextContent("本服務可能因你透過 Aviasales 連結訂購而獲得佣金。");
    const row = item("台北 → 東京");
    expect(row).toHaveTextContent("本服務可能因你透過 Aviasales 連結訂購而獲得佣金。");
    expect(within(row).getByText("連結與價格說明")).toBeInTheDocument();
    expect(row).toHaveTextContent("實際價格以訂購網站為準");
  });

  it("shows no-data as no data (never NT$0) and an API error as temporary", async () => {
    renderPage();
    await loaded();
    const noData = item("台北 → 倫敦");
    expect(noData).toHaveTextContent("目前沒有 2027 年 2 月 的近期票價資料");
    expect(noData).toHaveTextContent("不代表沒有航班");
    expect(noData).toHaveTextContent("最後查詢：");
    expect(noData).not.toHaveTextContent(/NT\$0|約 NT\$/);
    expect(within(noData).queryAllByRole("link")).toHaveLength(0);
    const error = item("台北 → 雪梨");
    expect(error).toHaveTextContent("票價查詢暫時失敗（服務錯誤），會在下次排程自動重試。");
    expect(error).toHaveTextContent("最後查詢：");
    expect(error).not.toHaveTextContent(/約 NT\$/);
    expect(within(error).queryAllByRole("link")).toHaveLength(0);
  });
});

describe("search links and affiliate fallback", () => {
  it("distinguishes itinerary links from labelled general search pages", async () => {
    renderPage();
    await loaded();
    const tyo = item("台北 → 東京");
    const sky = within(tyo).getByRole("link", { name: /在 Skyscanner 搜尋此行程/ });
    expect(sky).toHaveAttribute("data-link-kind", "itinerary");
    expect(sky).toHaveTextContent(/^Skyscanner$/); // short visible label, full accessible name
    expect(sky.getAttribute("href")).toContain("/tpe/tyo/261211/261218/");
    expect(sky).toHaveAttribute("rel", "noopener noreferrer");
    expect(sky).toHaveAttribute("target", "_blank");
    expect(sky).toHaveAccessibleName("在 Skyscanner 搜尋此行程（另開新視窗）");
    const av = within(tyo).getByRole("link", {
      name: /Aviasales 搜尋此行程（票價來源・聯盟連結・英文介面）/,
    });
    expect(av).toHaveTextContent("Aviasales（票價來源・聯盟連結）"); // visibly labelled affiliate link
    expect(av.getAttribute("href")).toBe(
      "https://www.aviasales.com/search/TPE1112TYO18121?marker=000000",
    );
    const google = within(tyo).getByRole("link", { name: /Google 航班 一般搜尋頁/ });
    expect(google).toHaveAttribute("data-link-kind", "fallback");
    expect(google).toHaveTextContent("Google 航班 一般搜尋頁");

    // KHH-BKK is not verified on Skyscanner/Trip.com: both are labelled general search pages.
    const bkk = item("高雄 → 曼谷");
    for (const name of [/前往 Skyscanner 一般搜尋頁/, /前往 Trip.com 一般搜尋頁/]) {
      const l = within(bkk).getByRole("link", { name });
      expect(l).toHaveAttribute("data-link-kind", "fallback");
      expect(l).toHaveTextContent("一般搜尋頁");
      expect(l).toHaveAccessibleName(
        expect.stringContaining("未帶入行程，請自行輸入並確認航線與日期"),
      );
      expect(l.getAttribute("href")).not.toMatch(/bkk|2027/i);
      expect(l.className).not.toContain("bg-primary"); // never styled as an itinerary button
    }
    expect(bkk).toHaveTextContent("「一般搜尋頁」不會帶入此行程，需自行輸入並確認航線與日期。");
    expect(bkk).toHaveTextContent("各平台的價格可能與 Aviasales 參考票價不同");
    // Preview builds use a test marker and say so (once, on the page).
    expect(screen.getByRole("note", { name: "參考票價說明" })).toHaveTextContent(
      "測試用標記（marker=000000），不是正式聯盟連結",
    );
  });

  it("never puts personal data in links", async () => {
    renderPage({ mode: "app" });
    await loaded();
    for (const a of screen.getAllByRole("link")) {
      const href = a.getAttribute("href") ?? "";
      expect(href).not.toMatch(/@|%40|example\.com|eyJ|00000000-0000/);
    }
  });

  it("hides the amount and all links when no valid affiliate marker is available (fail closed)", async () => {
    const qc = new QueryClient();
    const client = new MockSubscriptionsClient({ now, scenario: "demo" });
    render(
      <QueryClientProvider client={qc}>
        <SubscriptionsManager client={client} cacheKey="t" now={now} marker={null} />
      </QueryClientProvider>,
    );
    await loaded();
    const tyo = item("台北 → 東京");
    expect(tyo).toHaveTextContent("已查到參考票價，但目前無法提供 Aviasales 聯盟連結");
    expect(tyo).not.toHaveTextContent("NT$8,062");
    expect(fact(tyo, "參考票價（來源：Aviasales）")).toBe("暫不顯示");
    expect(fact(tyo, "與目標價相比")).toBe("—");
    expect(screen.getByRole("note", { name: "參考票價說明" })).not.toHaveTextContent("marker=");
    expect(within(tyo).queryAllByRole("link")).toHaveLength(0);
    expect(tyo).toHaveTextContent("最後查詢：");
  });
});

describe("authentication compatibility", () => {
  it("shows the signed-in account and signs out through Supabase", async () => {
    const { router } = renderPage({ mode: "app" });
    await loaded();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("user@example.com");
    expect(screen.getByRole("link", { name: "返回" })).toHaveAttribute("href", "/app");
    fireEvent.click(screen.getByRole("button", { name: /Sign out/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/signin"));
    expect(signOut).toHaveBeenCalled();
  });

  it("works without a session in preview mode and is clearly marked as mock data", async () => {
    renderPage({ mode: "preview" });
    await loaded();
    expect(screen.getByRole("note", { name: "開發預覽" })).toHaveTextContent(
      "不會建立、修改或刪除任何真實訂閱",
    );
    expect(screen.getByRole("link", { name: "回首頁" })).toBeInTheDocument();
  });
});

describe("mobile layout and accessibility", () => {
  it("uses a compact two-column fact grid, short link labels and full-width buttons on phones", async () => {
    renderPage();
    await loaded();
    expect(createForm().querySelector(".grid")?.className).toMatch(
      /(^| )grid( |$).*sm:grid-cols-2/,
    );
    const tyo = item("台北 → 東京");
    expect(tyo.querySelector("dl")?.className).toContain("grid-cols-2");
    expect(tyo.querySelector("dl")?.className).toContain("sm:grid-cols-4");
    const sky = within(tyo).getByRole("link", { name: /Skyscanner/ });
    expect(sky.className).toContain("w-full");
    expect(sky.className).toContain("sm:w-auto");
    // Visible link texts stay short (no multi-line labels); details live in the accessible name.
    for (const a of within(tyo).getAllByRole("link")) {
      expect((a.textContent ?? "").length).toBeLessThanOrEqual(26);
    }
    // Long notes are collapsed by default.
    expect(within(tyo).getByText("連結與價格說明").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("main").className).toContain("px-4");
  });

  it("keeps edit, pause and delete reachable as named buttons on every card", async () => {
    renderPage();
    await loaded();
    for (const li of within(list()).getAllByRole("listitem")) {
      if (li.getAttribute("aria-label") === null) continue;
      const names = within(li)
        .getAllByRole("button")
        .map((b) => b.textContent?.trim());
      expect(names).toContain("刪除");
      expect(names.some((n) => n === "編輯" || n === "重新啟用")).toBe(true);
    }
    expect(within(item("台北 → 東京")).getByRole("button", { name: "暫停追蹤" })).toBeEnabled();
    expect(within(item("高雄 → 沖繩")).getByRole("button", { name: "恢復追蹤" })).toBeEnabled();
  });

  it("tells users that one route can track one departure month", async () => {
    renderPage();
    await loaded();
    expect(screen.getByRole("note", { name: "參考票價說明" })).toHaveTextContent(
      "每條航線目前只能追蹤一個出發月份",
    );
  });
});
