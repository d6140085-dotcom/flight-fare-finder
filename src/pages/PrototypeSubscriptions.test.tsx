import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import PrototypeSubscriptions from "./PrototypeSubscriptions";

function renderPage() {
  return render(
    <MemoryRouter>
      <PrototypeSubscriptions />
    </MemoryRouter>,
  );
}

const createForm = () => screen.getByRole("form", { name: "新增訂閱" });
const list = () => screen.getByRole("list");
const item = (name: string) => within(list()).getByRole("listitem", { name });

function fill(form: HTMLElement, origin: string, destination: string, target: string) {
  fireEvent.change(within(form).getByLabelText("出發地"), { target: { value: origin } });
  fireEvent.change(within(form).getByLabelText("目的地"), { target: { value: destination } });
  fireEvent.change(within(form).getByLabelText("目標價（新台幣）"), { target: { value: target } });
}

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
  it("is clearly labelled as a prototype and shows the demo subscriptions", () => {
    renderPage();
    expect(screen.getByRole("note")).toHaveTextContent("原型預覽");
    expect(screen.getByRole("note")).toHaveTextContent("不會建立、修改或刪除任何真實訂閱");
    expect(item("台北 → 東京")).toHaveTextContent("NT$9,000");
    expect(item("台北 → 東京")).toHaveTextContent("追蹤中");
    expect(item("高雄 → 沖繩")).toHaveTextContent("已暫停");
    expect(screen.getByText(/追蹤中 1 \/ 10/)).toBeInTheDocument();
  });

  it("lists the three origins and 25 destinations with Chinese and English names", () => {
    renderPage();
    const origin = within(createForm()).getByLabelText("出發地");
    const dest = within(createForm()).getByLabelText("目的地");
    expect(
      within(origin)
        .getAllByRole("option")
        .slice(1)
        .map((o) => (o as HTMLOptionElement).value),
    ).toEqual(["TPE", "KHH", "RMQ"]);
    expect(within(dest).getAllByRole("option")).toHaveLength(26);
    expect(within(dest).getByRole("option", { name: "東京 Tokyo（TYO）" })).toBeInTheDocument();
  });

  it("creates a subscription", () => {
    renderPage();
    fill(createForm(), "RMQ", "OKA", "5,500");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    expect(item("台中 → 沖繩")).toHaveTextContent("NT$5,500");
    expect(screen.getByText(/追蹤中 2 \/ 10/)).toBeInTheDocument();
  });

  it("shows clear messages for identical places, duplicates and bad prices", () => {
    renderPage();
    fill(createForm(), "TPE", "TYO", "0");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    const alert = within(createForm()).getByRole("alert");
    expect(alert).toHaveTextContent("你已經訂閱過「台北 → 東京」這條航線。");
    expect(alert).toHaveTextContent("目標價請輸入 1 到 1,000,000 之間的整數");
    expect(within(list()).getAllByRole("listitem")).toHaveLength(2);

    fill(createForm(), "", "", "");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    expect(within(createForm()).getByRole("alert")).toHaveTextContent("請選擇出發地。");
  });

  it("enforces the 10 active subscription limit", () => {
    renderPage();
    for (const code of ["OSA", "NGO", "FUK", "OKA", "SPK", "KMJ", "SEL", "PUS", "CJU"]) {
      fill(createForm(), "TPE", code, "8000");
      fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    }
    expect(screen.getByText(/追蹤中 10 \/ 10/)).toBeInTheDocument();
    fill(createForm(), "TPE", "HKG", "8000");
    fireEvent.click(within(createForm()).getByRole("button", { name: "開始追蹤" }));
    expect(within(createForm()).getByRole("alert")).toHaveTextContent("最多只能同時追蹤 10 條航線");

    // Resuming the paused demo row is blocked too.
    fireEvent.click(within(item("高雄 → 沖繩")).getByRole("button", { name: "恢復追蹤" }));
    expect(screen.getAllByRole("alert").at(-1)).toHaveTextContent("最多只能同時追蹤 10 條航線");
    expect(item("高雄 → 沖繩")).toHaveTextContent("已暫停");
  });

  it("edits the route and target price, rejecting a change onto an existing route", () => {
    renderPage();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "編輯" }));
    const edit = screen.getByRole("form", { name: "編輯訂閱" });
    fill(edit, "KHH", "OKA", "9000");
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(within(edit).getByRole("alert")).toHaveTextContent(
      "你已經訂閱過「高雄 → 沖繩」這條航線。",
    );

    fill(edit, "RMQ", "SEL", "7,000");
    fireEvent.click(within(edit).getByRole("button", { name: "儲存變更" }));
    expect(within(list()).queryByRole("listitem", { name: "台北 → 東京" })).toBeNull();
    expect(item("台中 → 首爾")).toHaveTextContent("NT$7,000");
  });

  it("pauses and resumes tracking", () => {
    renderPage();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "暫停追蹤" }));
    expect(item("台北 → 東京")).toHaveTextContent("已暫停");
    expect(screen.getByText(/追蹤中 0 \/ 10/)).toBeInTheDocument();
    fireEvent.click(within(item("台北 → 東京")).getByRole("button", { name: "恢復追蹤" }));
    expect(item("台北 → 東京")).toHaveTextContent("追蹤中");
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
  });
});
