import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import PrototypeSubscriptions from "./PrototypeSubscriptions";

// 2026-11-05 11:00 Taipei: selectable months are 2026-12 … 2027-11.
const NOW = new Date("2026-11-05T03:00:00Z");

function renderPage() {
  return render(
    <MemoryRouter>
      <PrototypeSubscriptions now={() => NOW} />
    </MemoryRouter>,
  );
}

const createForm = () => screen.getByRole("form", { name: "新增訂閱" });
const list = () => screen.getByRole("list");
const item = (name: string) => within(list()).getByRole("listitem", { name });
const counter = (n: number) => screen.getByText(`我的航線訂閱：${n} / 10`);
const DEMO_ROWS = 5;

/** Search the destination picker and pick the best match with Enter, as a keyboard user would. */
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
  if (destination) pickDestination(form, destination);
  else {
    // Clearing the search box and leaving it clears the destination.
    const box = within(form).getByRole("combobox", { name: "目的地" });
    fireEvent.change(box, { target: { value: "" } });
    fireEvent.blur(box);
  }
  if (month !== undefined) {
    fireEvent.change(within(form).getByLabelText("出發月份"), { target: { value: month } });
  }
  fireEvent.change(within(form).getByLabelText("目標價（新台幣）"), { target: { value: target } });
}
const submitCreate = () =>
  fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn(() => Promise.reject(new Error("network disabled in prototype tests")));
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // No test may ever reach the network: the prototype is local-state only.
  expect(fetchSpy).not.toHaveBeenCalled();
});

describe("M1.4 subscription prototype", () => {
  it("is labelled as a prototype and shows active, paused, expired and long-haul demo rows", () => {
    renderPage();
    expect(screen.getByRole("note")).toHaveTextContent("原型預覽");
    expect(screen.getByRole("note")).toHaveTextContent("不會建立、修改或刪除任何真實訂閱");
    expect(item("台北 → 東京")).toHaveTextContent("追蹤中");
    expect(item("台北 → 東京")).toHaveTextContent("每月滾動（目前查詢 2026 年 12 月）");
    expect(item("台北 → 東京")).toHaveTextContent("NT$9,000");
    expect(item("高雄 → 沖繩")).toHaveTextContent("已暫停");
    expect(item("高雄 → 沖繩")).toHaveTextContent("2027 年 1 月");
    expect(item("台中 → 首爾")).toHaveTextContent("已過期");
    expect(item("台北 → 倫敦")).toHaveTextContent("Taipei → London");
    expect(item("台北 → 雪梨")).toHaveTextContent("2026 年 12 月");
    // Every record counts, whatever its status.
    expect(counter(DEMO_ROWS)).toBeInTheDocument();
  });

  it("distinguishes fare data, no recent data, API error, paused and expired", () => {
    renderPage();
    const fare = (name: string) => item(name).querySelector("[data-fare-kind]")!;
    expect(fare("台北 → 東京")).toHaveAttribute("data-fare-kind", "ok");
    expect(fare("台北 → 東京")).toHaveTextContent("最低 NT$9,820");
    expect(fare("台北 → 倫敦")).toHaveAttribute("data-fare-kind", "no_data");
    expect(fare("台北 → 倫敦")).toHaveTextContent("不代表沒有航班");
    expect(fare("台北 → 雪梨")).toHaveAttribute("data-fare-kind", "error");
    expect(fare("台北 → 雪梨")).toHaveTextContent("票價查詢暫時失敗");
    expect(fare("高雄 → 沖繩")).toHaveAttribute("data-fare-kind", "paused");
    expect(fare("台中 → 首爾")).toHaveAttribute("data-fare-kind", "expired");
  });

  it("offers the three origins and a searchable destination picker instead of a long dropdown", () => {
    renderPage();
    const origin = within(createForm()).getByLabelText("出發地");
    expect(
      within(origin)
        .getAllByRole("option")
        .slice(1)
        .map((o) => (o as HTMLOptionElement).value),
    ).toEqual(["TPE", "KHH", "RMQ"]);
    const dest = within(createForm()).getByLabelText("目的地");
    expect(dest).toHaveAttribute("role", "combobox");
    expect(dest.tagName).toBe("INPUT");
  });

  it("offers the next 12 months, defaulting to next month", () => {
    renderPage();
    const month = within(createForm()).getByLabelText("出發月份") as HTMLSelectElement;
    const values = within(month)
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    expect(values).toHaveLength(12);
    expect(values[0]).toBe("2026-12");
    expect(values.at(-1)).toBe("2027-11");
    expect(month.value).toBe("2026-12");
  });

  it.each([
    ["Europe", "TPE", "巴黎", "台北 → 巴黎", "Taipei → Paris"],
    ["North America", "KHH", "new york", "高雄 → 紐約", "Kaohsiung → New York"],
    ["South America", "TPE", "sao paulo", "台北 → 聖保羅", "Taipei → São Paulo"],
    ["Australia", "RMQ", "MEL", "台中 → 墨爾本", "Taichung → Melbourne"],
    ["New Zealand", "TPE", "皇后鎮", "台北 → 皇后鎮", "Taipei → Queenstown"],
  ])("creates a subscription to %s", (_region, origin, query, zh, en) => {
    renderPage();
    fill(createForm(), origin, query, "32,000", "2027-03");
    submitCreate();
    expect(item(zh)).toHaveTextContent(en);
    expect(item(zh)).toHaveTextContent("NT$32,000");
    expect(item(zh)).toHaveTextContent("2027 年 3 月");
    expect(item(zh)).toHaveTextContent("尚未查詢");
    expect(counter(DEMO_ROWS + 1)).toBeInTheDocument();
  });

  it("shows clear messages for duplicates (even with another month), identical places and bad prices", () => {
    renderPage();
    fill(createForm(), "TPE", "LON", "0", "2027-05");
    submitCreate();
    const alert = within(createForm()).getByRole("alert");
    expect(alert).toHaveTextContent("你已經訂閱過「台北 → 倫敦」這條航線");
    expect(alert).toHaveTextContent("每條航線只能訂閱一個月份");
    expect(alert).toHaveTextContent("目標價請輸入 1 到 1,000,000 之間的整數");
    expect(within(list()).getAllByRole("listitem")).toHaveLength(DEMO_ROWS);

    fill(createForm(), "", "", "");
    submitCreate();
    expect(within(createForm()).getByRole("alert")).toHaveTextContent("請選擇出發地。");
    expect(within(createForm()).getByRole("alert")).toHaveTextContent("請選擇目的地。");
  });

  it("enforces 10 total records (paused and expired count); pausing frees nothing, deleting frees one", () => {
    renderPage();
    for (const q of ["PAR", "NYC", "AKL", "SAO", "YVR"]) {
      fill(createForm(), "TPE", q, "30000");
      submitCreate();
    }
    expect(counter(10)).toBeInTheDocument();

    fill(createForm(), "TPE", "ZQN", "30000");
    submitCreate();
    expect(within(createForm()).getByRole("alert")).toHaveTextContent("最多 10 筆航線訂閱");
    expect(within(list()).queryByRole("listitem", { name: "台北 → 皇后鎮" })).toBeNull();

    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "暫停追蹤" }));
    submitCreate();
    expect(within(createForm()).getByRole("alert")).toHaveTextContent("最多 10 筆航線訂閱");
    fireEvent.click(within(item("高雄 → 沖繩")).getByRole("button", { name: "恢復追蹤" }));
    expect(item("高雄 → 沖繩")).toHaveTextContent("追蹤中");
    expect(counter(10)).toBeInTheDocument();

    fireEvent.click(within(item("台中 → 首爾")).getByRole("button", { name: "刪除" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("釋出 1 筆訂閱額度");
    fireEvent.click(screen.getByRole("button", { name: "確認刪除" }));
    expect(counter(9)).toBeInTheDocument();
    fill(createForm(), "TPE", "ZQN", "30000");
    submitCreate();
    expect(item("台北 → 皇后鎮")).toBeInTheDocument();
    expect(counter(10)).toBeInTheDocument();
  });

  it("keeps a legacy rolling row rolling when only the target changes", () => {
    renderPage();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    const edit = screen.getByRole("form", { name: "編輯訂閱" });
    const month = within(edit).getByLabelText("出發月份") as HTMLSelectElement;
    expect(month.value).toBe("rolling");
    expect(within(month).getAllByRole("option")).toHaveLength(13);
    expect(within(edit).getByRole("combobox", { name: "目的地" })).toHaveValue("東京 Tokyo（TYO）");
    fireEvent.change(within(edit).getByLabelText("目標價（新台幣）"), {
      target: { value: "8500" },
    });
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(item("台北 → 東京")).toHaveTextContent("每月滾動");
    expect(item("台北 → 東京")).toHaveTextContent("NT$8,500");
  });

  it("moves a legacy row to a fixed month, after which rolling is no longer offered", () => {
    renderPage();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    let edit = screen.getByRole("form", { name: "編輯訂閱" });
    fireEvent.change(within(edit).getByLabelText("出發月份"), { target: { value: "2027-02" } });
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(item("台北 → 東京")).toHaveTextContent("2027 年 2 月");
    expect(item("台北 → 東京")).not.toHaveTextContent("每月滾動");
    // The demo fare result was for December; February hasn't been checked yet.
    expect(item("台北 → 東京")).toHaveTextContent("尚未查詢 2027 年 2 月 的票價");

    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    edit = screen.getByRole("form", { name: "編輯訂閱" });
    const values = within(within(edit).getByLabelText("出發月份"))
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    expect(values).not.toContain("rolling");
    expect(values).toHaveLength(12);
  });

  it("reactivates an expired subscription by choosing a future month", () => {
    renderPage();
    const expired = item("台中 → 首爾");
    expect(within(expired).queryByRole("button", { name: "暫停追蹤" })).toBeNull();
    fireEvent.click(within(expired).getByRole("button", { name: "重新啟用" }));
    const edit = screen.getByRole("form", { name: "編輯訂閱" });
    expect(
      within(edit).getByRole("option", { name: "2026 年 10 月（已過期）" }),
    ).toBeInTheDocument();
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(item("台中 → 首爾")).toHaveTextContent("已過期");

    fireEvent.click(within(item("台中 → 首爾")).getByRole("button", { name: "重新啟用" }));
    const again = screen.getByRole("form", { name: "編輯訂閱" });
    fireEvent.change(within(again).getByLabelText("出發月份"), { target: { value: "2027-04" } });
    fireEvent.click(within(again).getByRole("button", { name: "儲存變更" }));
    expect(item("台中 → 首爾")).toHaveTextContent("追蹤中");
    expect(item("台中 → 首爾")).toHaveTextContent("2027 年 4 月");
    expect(counter(DEMO_ROWS)).toBeInTheDocument();
  });

  it("edits the route with the picker, rejecting a change onto an existing route", () => {
    renderPage();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    const edit = screen.getByRole("form", { name: "編輯訂閱" });
    fill(edit, "TPE", "倫敦", "9000");
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(within(edit).getByRole("alert")).toHaveTextContent(
      "你已經訂閱過「台北 → 倫敦」這條航線",
    );

    fill(edit, "RMQ", "Christchurch", "27,000", "2027-01");
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(within(list()).queryByRole("listitem", { name: "台北 → 東京" })).toBeNull();
    expect(item("台中 → 基督城")).toHaveTextContent("NT$27,000");
    expect(counter(DEMO_ROWS)).toBeInTheDocument();
  });

  it("deletes only after confirmation", () => {
    renderPage();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "刪除" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(item("台北 → 東京")).toBeInTheDocument();

    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "刪除" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      "確定要刪除「台北 → 東京」的降價追蹤嗎？",
    );
    fireEvent.click(screen.getByRole("button", { name: "確認刪除" }));
    expect(within(list()).queryByRole("listitem", { name: "台北 → 東京" })).toBeNull();
    expect(counter(DEMO_ROWS - 1)).toBeInTheDocument();
  });
});
