import { render, screen } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import TradeStatsView from "./TradeStats";

const stats = {
  synced: true,
  since: "2025-01-06",
  by_kind: {
    all: { trades: 20, win_rate: 0.45, median_return: 0.06, weighted_return: 0.031, avg_win: 0.42, avg_loss: -0.28, total_pnl: 1250, profit_factor: 1.4, median_days: 12 },
    calls: { trades: 14, win_rate: 0.43, median_return: 0.05, avg_win: 0.55, avg_loss: -0.35, median_days: 9, median_days_winners: 4, median_days_losers: 18, expired: 3, total_pnl: 600 },
    puts: { trades: 0 },
    shares: { trades: 6, win_rate: 0.5, median_return: 0.07, median_days: 70, median_days_winners: 90, median_days_losers: 40, expired: 0, total_pnl: 650 },
    options_sold: { trades: 0 },
  },
  holding: { all: [{ bucket: "same day", trades: 2 }, { bucket: "1-7 days", trades: 8 }, { bucket: "1-4 weeks", trades: 6 }, { bucket: "1-3 months", trades: 4 }] },
  monthly: [{ month: "2025-11", pnl: 400, trades: 5 }, { month: "2025-12", pnl: -150, trades: 4 }],
  best: [{ label: "XYZ Jan 16 '26 $50 Call", kind: "call", side: "long", opened: "2025-11-03", closed: "2025-11-10", days: 7, pnl: 520, return_pct: 1.3, exit: "sold" }],
  worst: [{ label: "ABC Dec 19 '25 $40 Call", kind: "call", side: "long", opened: "2025-11-12", closed: "2025-12-19", days: 37, pnl: -300, return_pct: -1, exit: "expired" }],
  open: { positions: 5, median_age_days: 21, oldest_days: 200 },
  unknown_cost: 2,
  observations: ["You hold losing calls 4.5x longer than winning ones (18 vs 4 days): cutting losers sooner is the usual fix."],
};

afterEach(() => vi.unstubAllGlobals());

describe("TradeStatsView", () => {
  it("shows how you trade: win rate, typical return, holding times by kind", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(stats)))));
    render(<TradeStatsView />);
    expect(await screen.findByText("45%")).toBeInTheDocument();
    expect(screen.getByText("+6%")).toBeInTheDocument(); // typical (median) return
    expect(screen.getByText(/You hold losing calls 4.5x longer/)).toBeInTheDocument();
    const calls = screen.getByRole("cell", { name: "Calls" }).closest("tr")!;
    expect(calls).toHaveTextContent("14");
    expect(calls).toHaveTextContent("9 days");
    expect(calls).toHaveTextContent("4 days"); // winners
    expect(calls).toHaveTextContent("18 days"); // losers
    expect(screen.queryByRole("cell", { name: "Puts" })).not.toBeInTheDocument(); // no puts traded
    expect(screen.getByText("XYZ Jan 16 '26 $50 Call")).toBeInTheDocument();
    expect(screen.getByText(/5 positions still open/)).toBeInTheDocument();
    // holding buckets keep their time order and show counts, not percentages of the largest
    const bars = [...document.querySelectorAll(".bar-row")].map((r) => r.textContent);
    expect(bars).toEqual(["same day2 · 10%", "1-7 days8 · 40%", "1-4 weeks6 · 30%", "1-3 months4 · 20%"]);
    expect(screen.getByText(/2 sales of positions bought before the synced history/)).toBeInTheDocument();
  });

  it("explains what to do before there is any history", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ synced: false })))));
    render(<TradeStatsView />);
    expect(await screen.findByText(/tp-broker link/)).toBeInTheDocument();
  });
});
