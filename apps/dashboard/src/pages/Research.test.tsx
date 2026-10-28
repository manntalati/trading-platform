import { fireEvent, render, screen } from "@testing-library/react";
import type {
  CalendarPick,
  ContractPick,
  CreditSpreadPick,
  IronCondorPick,
  OptionSummary,
  PlayPick,
  ResearchIdea,
  SpreadPick,
  StockPick,
  TrackedRecommendation,
} from "../types";
import { fitLabel, skewLine } from "./Options";
import { BudgetLine, HoldingAlerts, OutOfReachList } from "./Playbook";
import { BacktestCard, EarningsCard, IdeaCard, PickLine, PickSummary, TrackedIdeas } from "./Research";

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

const model = { expected_return: 0.12, sd: 0.6, sharpe: 0.2, prob_profit: 0.46, q10: -0.55, entry: 4.1, expected_exit: 4.6, capital: 4.1, horizon: 10, drift: 0.01, vol: 0.28 };
const play: PlayPick = {
  kind: "call debit spread",
  structure: "call debit spread",
  legs: [
    { ...call, side: "buy", gamma: 3.1 },
    { ...call, strike: 110, label: "XYZ Mar 13 '26 $110 Call", side: "sell", price: 2.0, gamma: 2.0 },
  ],
  expiration: "2026-03-13",
  dte: 60,
  debit: 4.1,
  credit: null,
  cost: 410,
  capital: 410,
  width: 10,
  max_loss: 410,
  max_gain: 590,
  greeks: { delta: 21, gamma: 1.1, theta_day: -3.1, vega: 6, theta_pct: -0.0076 },
  breakeven_move: null,
  forecast_move: 0.017,
  model,
  mid: 4.0,
  entry: { limit: 4.0, side: "debit", worst: 4.6, valid_until: "2026-01-14", worth_it: true },
  exit: {
    rules: { take_profit: 0.5, stop_loss: -0.5, time_stop: 10, min_dte: 21, underlying: "above_ma5" },
    take_profit: 6.15,
    stop: 2.05,
    sell_by: "2026-01-26",
    out_by: "2026-02-20",
    stock: { rule: "above_ma5", text: "a close above the 5-day average", level: 101.4, side: "above" },
  },
  size: { contracts: 2, capital: 820, max_loss: 820, share_of_account: 0.0164, buying_power_left: 2180, over_guideline: false, limited_by: "risk" },
};

const idea: ResearchIdea = {
  symbol: "XYZ",
  kind: "swing",
  score: 0.82,
  spot: 102,
  setup: { direction: "bullish", tags: ["uptrend"], rs_3m: 0.06 },
  stock: { price: 102 },
  vol: { iv30: 0.32, hv20: 0.2, iv_hv: 1.6, rv_forecast: 0.22, iv_rv: 1.45, rank: null, percentile: null, history_days: 5, event: null, rich: true, as_of: null },
  play: { name: "pullback", title: "Pullback in an uptrend", rule: "RSI(2) under 10 in an uptrend", fired: "2026-01-12", age: 0, strength: 0.8, horizon: 10, drift: 0.003, drift_source: "market", backtest: {} },
  pick: play,
  alternative: { ...play, structure: "call", legs: play.legs.slice(0, 1), width: null, max_gain: null },
  why: ["Pullback in an uptrend (today): RSI(2) under 10 in an uptrend."],
  risks: ["Already held: 100 shares."],
  held: "100 shares",
};

describe("IdeaCard", () => {
  it("shows the setup, the position, the model's view and the plan in and out", () => {
    render(<IdeaCard idea={idea} />);
    expect(screen.getByRole("link", { name: "XYZ" })).toHaveAttribute("href", "#/research/lookup/XYZ");
    expect(screen.getByText("Pullback in an uptrend · today")).toBeInTheDocument();
    expect(screen.getByText(/options expensive · IV 1.5x forecast/)).toBeInTheDocument();
    expect(screen.getByText("max gain $590.00")).toBeInTheDocument();
    expect(screen.getByText(/Sell XYZ Mar 13 '26 \$110 Call at \$2.00/)).toBeInTheDocument();
    expect(screen.getByText("+12% expected")).toBeInTheDocument();
    expect(screen.getByText(/limit \$4.00 debit \(the mid\)/)).toBeInTheDocument();
    expect(screen.getByText("$4.60")).toBeInTheDocument(); // pay no more than
    expect(screen.getByText(/take profit at \$6.15 \(\+50%\)/)).toBeInTheDocument();
    expect(screen.getByText(/or on a close above the 5-day average \(now \$101.40\)/)).toBeInTheDocument();
    expect(screen.getByText("2026-01-26")).toBeInTheDocument(); // out by
    expect(screen.getByText("Already held: 100 shares.")).toBeInTheDocument();
    expect(screen.getByText(/Alternative:/)).toBeInTheDocument();
    expect(screen.getByText("2 contracts")).toBeInTheDocument();
    expect(screen.getByText("$820.00 · 1.6% at risk")).toBeInTheDocument();
  });
});

describe("BudgetLine and OutOfReachList", () => {
  it("says what the ideas are sized for, and what didn't fit", () => {
    const budget = { buying_power: 3000, account_value: 50_000, account: "Individual …1234", risk_per_trade: 0.02, risk_cap: 1000, options_level: 2, as_of: null, paper: false };
    render(
      <p>
        <BudgetLine budget={budget} />
      </p>,
    );
    expect(screen.getByText("Individual …1234")).toBeInTheDocument();
    expect(screen.getByText("$3,000")).toBeInTheDocument();
    expect(screen.getByText(/options level 2/)).toBeInTheDocument();
    render(
      <OutOfReachList
        budget={budget}
        items={[
          { symbol: "XYZ", list: "leaps", setup: "Trend leader at its 50-day", structure: "LEAPS call", needs: 8400, reason: "buying power", level: 2, score: 0.3 },
          { symbol: "ABC", list: "income", setup: "Expensive options: sell premium", structure: "put credit spread", needs: 400, reason: "options level", level: 3, score: 0.2 },
        ]}
      />,
    );
    expect(screen.getByText(/Out of reach with \$3,000 of buying power/)).toBeInTheDocument();
    expect(screen.getByText(/the cheapest fit, a LEAPS call, needs \$8,400/)).toBeInTheDocument();
    expect(screen.getByText(/needs options level 3/)).toBeInTheDocument();
  });

  it("asks for a sync before sizing", () => {
    render(
      <p>
        <BudgetLine budget={null} />
      </p>,
    );
    expect(screen.getByText(/Not sized to an account yet/)).toBeInTheDocument();
  });
});

describe("PickSummary", () => {
  it("shows a debit spread's capped gain (Look up)", () => {
    render(<PickSummary pick={spread} />);
    expect(screen.getByText("$590.00")).toBeInTheDocument();
    expect(screen.getByText("1.4x the cost")).toBeInTheDocument();
  });

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

describe("BacktestCard", () => {
  it("shows each setup's backtest, or how to make one", () => {
    const { rerender } = render(<BacktestCard c={{ setups: {} }} />);
    expect(screen.getByText(/Not calibrated yet/)).toBeInTheDocument();
    rerender(
      <BacktestCard
        c={{
          generated_at: "2026-10-24T13:00:00+00:00",
          start: "2019-01-02",
          end: "2026-10-23",
          names: 104,
          model: "Black-Scholes at 1.15x an EWMA volatility forecast",
          setups: {
            pullback: {
              title: "Pullback in an uptrend",
              horizon: 10,
              events: 812,
              baseline: 0.002,
              drift: 0.006,
              underlying: { mean: 0.007, median: 0.008, hit_rate: 0.61, excess: 0.004 },
              exits: { take_profit: 0.5, stop_loss: -0.5, time_stop: 10, underlying: "above_ma5" },
              option: { trades: 320, mean: 0.06, median: 0.04, win_rate: 0.54, detail: { default: { trades: 320, mean: 0.03 } } },
              real: { trades: 41, mean: 0.02, model_bias: 0.03 },
            },
          },
        }}
      />,
    );
    expect(screen.getByText("Pullback in an uptrend")).toBeInTheDocument();
    expect(screen.getByText("812")).toBeInTheDocument();
    expect(screen.getByText("61%")).toBeInTheDocument();
    expect(screen.getByText(/exits: \+50% · −50% · 10 sessions · above ma5/)).toBeInTheDocument();
    expect(screen.getByText("of 41")).toBeInTheDocument();
  });
});

function tracked(i: number, state: "open" | "closed"): TrackedRecommendation {
  return {
    list: i % 2 ? "leaps" : "swing",
    symbol: `S${String(i).padStart(2, "0")}`,
    setup: "pullback",
    structure: "call",
    recommended_at: "2026-10-01T15:00:00+00:00",
    last_seen: i === 0 ? "2026-10-03T19:00:00+00:00" : "2026-10-01T15:00:00+00:00",
    runs: i === 0 ? 14 : 1,
    contracts: i === 0 ? 3 : 1,
    rank: 1,
    spot: 100,
    legs: [],
    entry: 2,
    move: 0.01,
    move_vs_spy: 0,
    move_5d: null,
    move_21d: null,
    value: 2.2,
    pnl: 20,
    result: 0.1,
    marked: null,
    state,
  };
}

describe("TrackedIdeas", () => {
  const ideas = Array.from({ length: 40 }, (_, i) => tracked(i, i < 30 ? "open" : "closed"));
  const record = { as_of: "2026-10-28", total: 40, recommendations: ideas, summary: [] };

  it("shows each idea once, a page at a time", () => {
    render(<TrackedIdeas t={record} />);
    expect(screen.getByText("40 ideas, 30 open")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(16); // the header and the first 15
    expect(screen.getByTitle(/listed in 14 runs until 2026-10-03, with 3 different contracts; tracked with the first/)).toHaveTextContent("14 runs to 10-03");
    fireEvent.click(screen.getByRole("button", { name: "Show 15 more of 25" }));
    expect(screen.getAllByRole("row")).toHaveLength(31);
  });

  it("filters to the open or the closed ones", () => {
    render(<TrackedIdeas t={record} />);
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "closed" } });
    expect(screen.getAllByRole("row")).toHaveLength(11);
    expect(screen.queryByRole("button", { name: /more/ })).not.toBeInTheDocument();
  });
});

const shares: StockPick = {
  kind: "shares",
  structure: "shares",
  price: 50,
  atr: 1.2,
  risk_per_share: 3,
  entry: { side: "debit", limit: 50, worst: 50.6, valid_until: "2026-10-30", worth_it: true },
  exit: {
    stop: 47,
    target: null,
    sell_by: "2026-11-11",
    stock: { rule: "above_ma5", text: "a close above the 5-day average", level: 51.2, side: "above" },
    rules: { stop_atr: 2.5, target_atr: null, time_stop: 10, underlying: "above_ma5" },
  },
  model: { horizon: 10, drift: 0.006, drift_source: "backtest", vol: 0.3, expected_return: 0.006, sharpe: 0.09, stop_pct: 0.06, target_pct: null },
  backtest: { trades: 140, win_rate: 0.58, mean: 0.011, avg_sessions: 4.2, excess: 0.008, mean_r: 0.31 },
  size: { shares: 40, capital: 2000, max_loss: 120, share_of_account: 0.006, buying_power_left: 1000, over_guideline: false, limited_by: "buying power" },
};

describe("stock ideas", () => {
  it("shows the shares, the stop, the time stop and the size", () => {
    render(<IdeaCard idea={{ ...idea, kind: "stocks", list: "stocks", pick: shares, alternative: null }} />);
    expect(screen.getByText("Shares")).toBeInTheDocument();
    expect(screen.getByText("+0.6% drift")).toBeInTheDocument();
    expect(screen.getByText("40 shares")).toBeInTheDocument();
    expect(screen.getByText("$2,000 · $120.00 to the stop")).toBeInTheDocument();
    expect(screen.getByText("58% won")).toBeInTheDocument();
    expect(screen.getByText(/or on a close above the 5-day average \(now \$51\.20\)/)).toBeInTheDocument();
    expect(screen.getByText("2026-11-11")).toBeInTheDocument();
    render(<PickLine pick={shares} />);
    expect(screen.getByText(/shares at \$50\.00, stop \$47\.00 \(40 shares\)/)).toBeInTheDocument();
  });

  it("lists downward setups on what you hold, and shares out of reach", () => {
    render(
      <HoldingAlerts
        items={[{ symbol: "XYZ", held: "40 shares", setup: "breakdown", title: "Breakdown to a new low", fired: "2026-10-27", age: 0, spot: 40, text: "Breakdown to a new low (today)." }]}
      />,
    );
    expect(screen.getByText("On what you hold")).toBeInTheDocument();
    expect(screen.getByText("40 shares")).toBeInTheDocument();
    render(
      <OutOfReachList
        items={[{ symbol: "ABC", list: "stocks", setup: "Breakout from a squeeze", structure: "shares", needs: 812.5, reason: "buying power", level: 0, score: 1 }]}
        budget={null}
      />,
    );
    expect(screen.getByText(/a share costs \$812\.50/)).toBeInTheDocument();
  });
});
