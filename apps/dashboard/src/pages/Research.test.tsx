import { render, screen } from "@testing-library/react";
import type { CalendarPick, ContractPick, CreditSpreadPick, IronCondorPick, OptionSummary, ResearchIdea, SpreadPick } from "../types";
import { fitLabel, skewLine } from "./Options";
import { EarningsCard, IdeaCard, PickLine, PickSummary } from "./Research";

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

const put: ContractPick = { ...call, contract: "XYZ260313P00095000", label: "XYZ Mar 13 '26 $95 Put", right: "put", strike: 95, price: 1.8, delta: -0.25, theta_day: -4.2 };

const putSpread: CreditSpreadPick = {
  kind: "put credit spread",
  short: put,
  long: { ...put, strike: 90, label: "XYZ Mar 13 '26 $90 Put" },
  credit: 1.1,
  premium: 110,
  width: 5,
  max_loss: 390,
  return_on_risk: 0.28,
  breakeven: 93.9,
  breakeven_move_pct: -0.08,
  prob_profit: 0.71,
  theta_day: 2.2,
  dte: 35,
};

describe("selling premium", () => {
  it("shows a cash-secured put as a sale with the cash it ties up", () => {
    const csp = { ...put, premium: 180, cash_required: 9500, yield_pct: 0.019, annualized_pct: 0.2, net_entry: 93.2, net_entry_discount_pct: -0.086, prob_assigned: 0.24 };
    render(<PickSummary pick={csp} />);
    expect(screen.getByText(/\$9,500 set aside/)).toBeInTheDocument();
    expect(screen.getByText("+$4.20/day")).toBeInTheDocument(); // the seller earns the decay
    expect(screen.getByText("24%")).toBeInTheDocument();
  });

  it("shows a credit spread's credit, maximum loss and odds, not a debit spread's", () => {
    render(<PickSummary pick={putSpread} />);
    expect(screen.getByText("$390.00")).toBeInTheDocument();
    expect(screen.getByText("71%")).toBeInTheDocument();
    render(<PickLine pick={putSpread} />);
    expect(screen.getByText(/put credit spread \$95\/\$90/)).toBeInTheDocument();
  });

  it("shows an iron condor's range and a calendar's best case", () => {
    const condor: IronCondorPick = {
      kind: "iron condor",
      put_spread: putSpread,
      call_spread: { ...putSpread, kind: "call credit spread", short: { ...call, strike: 110 }, long: { ...call, strike: 115 } },
      credit: 2.0,
      premium: 200,
      max_loss: 300,
      return_on_risk: 0.67,
      lower_breakeven: 93,
      upper_breakeven: 112,
      range_pct: [-0.09, 0.1],
      prob_profit: 0.55,
      theta_day: 4.1,
      dte: 35,
    };
    render(<PickSummary pick={condor} />);
    expect(screen.getByText("$93.00–$112.00")).toBeInTheDocument();
    const cal: CalendarPick = {
      kind: "calendar",
      front: call,
      back: { ...call, expiration: "2026-05-15" },
      strike: 100,
      debit: 1.9,
      cost: 190,
      max_profit_est: 250,
      profit_range: [93, 108],
      prob_profit: 0.45,
      theta_day: 2.7,
      vega: 5,
      front_iv: 0.34,
      back_iv: 0.31,
    };
    render(<PickSummary pick={cal} />);
    expect(screen.getByText("$250.00")).toBeInTheDocument();
    expect(screen.getByText("34% / 31%")).toBeInTheDocument();
  });
});

describe("smile in words", () => {
  it("describes skew, term structure and bad quotes", () => {
    const v = {
      underlying: "XYZ",
      snapshot_at: null,
      term: [],
      smile: [],
      skew: { skew_25d: -0.052, butterfly_25d: 0.004, iv30: 0.3, iv90: 0.27, ratio: 0.9, shape: "backwardation" },
      quote_arbitrage: { vertical: 1, butterfly: 0 },
    } as OptionSummary;
    expect(skewLine(v)).toBe(
      "25-delta puts trade 5.2 volatility points over calls; 90-day IV 0.90x the 30-day (near-term options dearer: the market expects a rough few weeks); 1 call quotes inconsistent with each other (stale or bad quotes).",
    );
    expect(fitLabel(null)).toBe("too few quotes");
  });
});

describe("EarningsCard", () => {
  it("shows the next results date, the last surprise and valuation", () => {
    render(
      <EarningsCard
        f={{
          period_end: "2024-09-30",
          last_release: "2024-10-28",
          next_release_estimate: "2025-01-29",
          eps_last_quarter: 1.52,
          eps_ttm: 5.9,
          pe: 18.4,
          earnings_yield: 0.054,
          book_to_market: 0.21,
          gross_profitability: 0.33,
          roe: 0.29,
          market_cap: 1e11,
          sue: 2.4,
        }}
      />,
    );
    expect(screen.getByText("2025-01-29")).toBeInTheDocument();
    expect(screen.getByText("last: 2024-10-28")).toBeInTheDocument();
    expect(screen.getByText("+2.4σ")).toBeInTheDocument();
    expect(screen.getByText("18.4")).toBeInTheDocument();
  });
});
