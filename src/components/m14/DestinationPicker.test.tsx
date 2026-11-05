import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";

import { DestinationPicker } from "./DestinationPicker";

function Harness({ onSubmit = () => {} }: { onSubmit?: () => void }) {
  const [value, setValue] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <label htmlFor="dest">目的地</label>
      <DestinationPicker id="dest" value={value} onChange={setValue} />
      <output data-testid="value">{value}</output>
    </form>
  );
}

const input = () => screen.getByRole("combobox", { name: "目的地" });
const listbox = () => screen.getByRole("listbox", { name: "目的地" });
const active = () => {
  const id = input().getAttribute("aria-activedescendant");
  return id ? document.getElementById(id)?.getAttribute("aria-label") : null;
};
const key = (k: string) => fireEvent.keyDown(input(), { key: k });
const type = (text: string) => fireEvent.change(input(), { target: { value: text } });
const value = () => screen.getByTestId("value").textContent;

afterEach(cleanup);

describe("DestinationPicker", () => {
  it("is a labelled combobox that opens a listbox grouped by region", () => {
    render(<Harness />);
    expect(input()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).toBeNull();
    key("ArrowDown");
    expect(input()).toHaveAttribute("aria-expanded", "true");
    expect(input()).toHaveAttribute("aria-controls", listbox().id);
    const groups = within(listbox()).getAllByRole("group");
    expect(
      groups.map(
        (g) =>
          g.getAttribute("aria-labelledby") &&
          document.getElementById(g.getAttribute("aria-labelledby")!)?.textContent,
      ),
    ).toEqual([
      "亞洲 Asia",
      "中東 Middle East",
      "歐洲 Europe",
      "北美洲 North America",
      "中南美洲 Central and South America",
      "澳洲 Australia",
      "紐西蘭 New Zealand",
    ]);
    const nz = groups[6]!;
    expect(
      within(nz)
        .getAllByRole("option")
        .map((o) => o.getAttribute("aria-label")),
    ).toEqual([
      "奧克蘭 Auckland（AKL）",
      "基督城 Christchurch（CHC）",
      "威靈頓 Wellington（WLG）",
      "皇后鎮 Queenstown（ZQN）",
    ]);
  });

  it("shows Chinese and English names, the IATA code, and which airports a city code covers", () => {
    render(<Harness />);
    type("倫敦");
    const lon = screen.getByRole("option", { name: "倫敦 London（LON）" });
    expect(lon).toHaveTextContent("倫敦");
    expect(lon).toHaveTextContent("London");
    expect(lon).toHaveTextContent("LON");
    expect(lon).toHaveTextContent("城市代碼，含 LHR、LGW");
    type("Frankfurt");
    expect(screen.getByRole("option", { name: "法蘭克福 Frankfurt（FRA）" })).not.toHaveTextContent(
      "城市代碼",
    );
  });

  it.each([
    ["巴黎", "巴黎 Paris（PAR）"],
    ["new york", "紐約 New York（NYC）"],
    ["SYD", "雪梨 Sydney（SYD）"],
    ["queenstown", "皇后鎮 Queenstown（ZQN）"],
  ])("highlights the best match for %j and selects it with Enter", (q, label) => {
    render(<Harness />);
    type(q);
    expect(active()).toBe(label);
    key("Enter");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input()).toHaveValue(label);
  });

  it("moves with the arrow keys (wrapping), Home and End", () => {
    render(<Harness />);
    type("an"); // matches many names (Bangkok, Hanoi, Manila, Milan, …)
    const labels = within(listbox())
      .getAllByRole("option")
      .map((o) => o.getAttribute("aria-label"));
    expect(labels.length).toBeGreaterThan(1);
    key("Home");
    expect(active()).toBe(labels[0]);
    key("ArrowDown");
    expect(active()).toBe(labels[1]);
    key("End");
    expect(active()).toBe(labels.at(-1));
    key("ArrowDown"); // wraps to the first
    expect(active()).toBe(labels[0]);
    key("ArrowUp"); // wraps to the last
    expect(active()).toBe(labels.at(-1));
  });

  it("selects with a click and marks the selected option", () => {
    render(<Harness />);
    type("墨爾本");
    fireEvent.click(screen.getByRole("option", { name: "墨爾本 Melbourne（MEL）" }));
    expect(value()).toBe("MEL");
    expect(input()).toHaveValue("墨爾本 Melbourne（MEL）");
    key("ArrowDown");
    expect(screen.getByRole("option", { name: "墨爾本 Melbourne（MEL）" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(active()).toBe("墨爾本 Melbourne（MEL）");
  });

  it("Escape closes without changing the selection; Enter while open never submits the form", () => {
    const submit = vi.fn();
    render(<Harness onSubmit={submit} />);
    type("AKL");
    key("Enter");
    expect(value()).toBe("AKL");
    type("Paris");
    key("Escape");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input()).toHaveValue("奧克蘭 Auckland（AKL）");
    expect(value()).toBe("AKL");
    expect(submit).not.toHaveBeenCalled();
  });

  it("says so when nothing matches, and Enter then selects nothing", () => {
    render(<Harness />);
    type("Atlantis");
    expect(within(listbox()).queryAllByRole("option")).toHaveLength(0);
    expect(listbox()).toHaveTextContent("找不到「Atlantis」");
    expect(screen.getByText("找不到符合的目的地")).toBeInTheDocument();
    key("Enter");
    expect(value()).toBe("");
  });

  it("clears the destination when the box is emptied", () => {
    render(<Harness />);
    type("AKL");
    key("Enter");
    expect(value()).toBe("AKL");
    type("");
    expect(active()).toBeNull();
    key("Enter");
    expect(value()).toBe("");
    expect(input()).toHaveValue("");
  });

  it("closes on blur and restores the selected label", () => {
    render(<Harness />);
    type("SYD");
    key("Enter");
    type("xyz");
    fireEvent.blur(input());
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input()).toHaveValue("雪梨 Sydney（SYD）");
  });
});
