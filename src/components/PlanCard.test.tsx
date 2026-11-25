import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { PLANS } from "@/lib/flight-api";
import { PlanCard } from "./PlanCard";

// B6 MVP safety: the M1 plan cards no longer show hard-coded "recent lowest fare" amounts, which
// were unsourced and stale. The card keeps its layout, target-price form and save flow.
describe("M1 plan card", () => {
  it("has no fare claim or amount before a target is set", () => {
    const { container } = render(<PlanCard plan={PLANS[0]} onSave={vi.fn()} />);
    expect(container).not.toHaveTextContent(/近期最低價|Recent lowest fare/);
    expect(container.textContent ?? "").not.toMatch(/NT\$\s?\d/); // no amount of any kind
    expect(
      screen.getByText(/設定你的目標價（新台幣），找到低於目標價的票價時以 email 通知你。/),
    ).toBeVisible();
    const input = screen.getByLabelText("目標價 Target price (TWD)");
    expect(input).toHaveAttribute("placeholder", "輸入目標價 Enter a target price");
    expect(input.getAttribute("placeholder") ?? "").not.toMatch(/\d/);
    expect(screen.getByRole("button", { name: "開始追蹤 Start tracking" })).toBeEnabled();
  });

  it("plans carry no fare amounts", () => {
    for (const plan of PLANS)
      expect(Object.keys(plan).sort()).toEqual(["label", "labelEn", "planName", "route"]);
  });

  it("still shows the saved target and saves a new one", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <PlanCard
        plan={PLANS[1]}
        subscription={{
          route: "TPE-SEL",
          plan_name: "seoul",
          target_price: 6000,
          currency: "TWD",
          updated_at: "2026-11-01T00:00:00Z",
        }}
        onSave={onSave}
      />,
    );
    expect(screen.getByText("NT$6,000")).toBeVisible(); // the user's own target, not a fare
    fireEvent.change(screen.getByLabelText("目標價 Target price (TWD)"), {
      target: { value: "5500" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更新目標價 Update target" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(5500));
  });
});
