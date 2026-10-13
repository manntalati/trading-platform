import { render, screen } from "@testing-library/react";
import type { ContractPick, ResearchIdea, SpreadPick } from "../types";
import { IdeaCard, PickSummary } from "./Research";

const call: ContractPick = {
  contract: "XYZ260313C00100000",
  label: "XYZ Mar 13 '26 $100 Call",
  right: "call",
  strike: 100,
  expiration: "2026-03-13",
  dte: 60,
  bid: 5.9,
  ask: 6.1,
  mid: 6,
  spread_pct: 0.033,
  open_interest: 1200,
  price: 6.1,
  cost: 610,
  iv: 0.32,
  delta: 0.56,
  theta_day: -6.4,
  vega: 16,
  intrinsic: 2,
  extrinsic: 4,
  breakeven: 106.1,
  breakeven_move_pct: 0.04,
  prob_itm: 0.52,
  prob_profit: 0.38,
  expected_move_pct: 0.13,
  leverage: 9.5,
};

const spread: SpreadPick = {
  long: call,
  short: { ...call, strike: 110, label: "XYZ Mar 13 '26 $110 Call", bid: 2.0 },
  debit: 4.1,
  cost: 410,
  max_gain: 590,
  reward_risk: 1.44,
  breakeven: 104.1,
  breakeven_move_pct: 0.02,
  prob_profit: 0.42,
  theta_day: -3.1,
};

const idea: ResearchIdea = {
  symbol: "XYZ",
  kind: "swing",
  score: 0.82,
  spot: 102,
  setup: { direction: "bullish", tags: ["uptrend", "pullback"], rs_3m: 0.06 },
  stock: { price: 102 },
  vol: { iv30: 0.32, hv20: 0.2, iv_hv: 1.6, rank: null, percentile: null, history_days: 5, event: null, rich: true, as_of: null },
  pick: spread,
  alternative: call,
  why: ["Uptrend: +3.0% vs its 50-day and +12.0% vs its 200-day average (price above both)."],
  risks: ["Already held: 100 shares."],
  held: "100 shares",
};

describe("IdeaCard", () => {
  it("shows the structure, the numbers, why and what to watch", () => {
    render(<IdeaCard idea={idea} />);
    expect(screen.getByRole("link", { name: "XYZ" })).toHaveAttribute("href", "#/research/lookup/XYZ");
    expect(screen.getByText("pullback")).toBeInTheDocument();
    expect(screen.getByText(/options expensive/)).toBeInTheDocument();
    expect(screen.getByText("$590.00")).toBeInTheDocument(); // max gain
    expect(screen.getByText(/Uptrend: \+3.0%/)).toBeInTheDocument();
    expect(screen.getByText("Already held: 100 shares.")).toBeInTheDocument();
    expect(screen.getByText(/Alternative:/)).toBeInTheDocument();
  });
});

describe("PickSummary", () => {
  it("prices a LEAPS against the shares it stands in for", () => {
    const leaps = { ...call, dte: 520, delta: 0.8, shares_equivalent: 80, capital_freed: 7500, stock_cost: 10200, carry_per_year_pct: 0.031, breakeven_cagr: 0.035 };
    render(<PickSummary pick={leaps} />);
    expect(screen.getByText("acts like 80 shares")).toBeInTheDocument();
    expect(screen.getByText("$7,500")).toBeInTheDocument();
    expect(screen.getByText("3.1%/yr")).toBeInTheDocument();
  });
});
