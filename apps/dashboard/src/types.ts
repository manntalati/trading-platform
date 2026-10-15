// Shapes returned by tp_api (services/api/src/tp_api/queries.py).

export type Num = number | null;

export interface Quote {
  symbol: string;
  price: Num;
  prev_close: Num;
  change_pct: Num;
  at: string | null;
  live: boolean;
}

export interface LivePortfolio {
  value: number;
  day_pnl: number;
  day_pnl_pct: Num;
}

export interface LiveMessage {
  type: "snapshot" | "update";
  at?: string;
  quotes: Quote[];
  portfolio: LivePortfolio | null;
  feed: { source: string; error: string | null };
}

export interface MarketClock {
  now: string;
  is_open: boolean;
  next_open: string;
  next_close: string;
  last_completed_session: string;
}

export interface Status {
  market: MarketClock;
  bars: {
    symbols: number;
    rows: number;
    first_session?: string;
    latest_session: string | null;
    stale?: boolean;
    expected_session?: string;
  };
  validation: (Record<string, unknown> & { ok?: boolean; counts?: Record<string, number>; generated_at?: string }) | null;
  options: { files: number; latest_report: (Record<string, unknown> & { snapshot_date?: string; ok?: boolean; skipped?: string | null }) | null };
  broker: { source: string; institution: string; account: string; synced_at: string }[];
  fundamentals?: (Record<string, unknown> & { ok?: boolean; facts?: Record<string, number>; errors?: Record<string, string>; no_cik?: string[] }) | null;
  macro?: {
    series: Record<string, { value: Num; date: string; name: string }>;
    latest_report: (Record<string, unknown> & { ok?: boolean; errors?: Record<string, string> }) | null;
  };
  universe: { bars: number; options: number; watchlist: string[] };
  live: { source: string; symbols: string[]; error: string | null };
  dashboard?: { built_at: string | null; source_changed_at: string | null; stale: boolean };
}

export interface Holding {
  symbol: string;
  label: string; // options: "XYZ Jan 16 '26 $50 Call"
  description: string | null;
  kind: string;
  underlying: string | null;
  quantity: Num;
  price: Num; // options: per contract
  market_value: Num;
  cost_basis_per_unit: Num;
  unrealized_pnl: Num;
  unrealized_pct: Num;
  weight: Num;
  sector: string;
  asset_class: string;
  priced: boolean;
  // options only
  multiplier?: Num;
  expiration?: string | null;
  strike?: Num;
  right?: "C" | "P" | null;
  days_to_expiry?: Num;
  premium?: Num; // per share, as brokers quote it
  cost_premium?: Num;
  underlying_price?: Num;
  breakeven?: Num;
  moneyness?: Num; // + in the money, - out of the money (fraction of the strike)
}

export interface PnlSummary {
  realized: number;
  realized_ytd: number;
  closed_trades: number;
  winners: number;
  win_rate: Num;
  unknown_cost: number;
  income: number;
  income_ytd: number;
  fees: number;
  net_deposits: number;
  since: string | null;
  by_symbol: Record<string, number>;
}

export interface PortfolioPnl extends PnlSummary {
  unrealized: number;
  unrealized_pct: Num;
  total: number;
  value: number;
  value_minus_deposits: Num;
}

export interface Trade {
  date: string;
  source: "broker" | "paper";
  account: string;
  strategy: string | null;
  symbol: string;
  label: string;
  kind: "equity" | "option";
  action: string;
  quantity: number;
  price: Num;
  amount: Num;
  fee: Num;
  realized_pnl: Num;
  cost_known: boolean;
  pending: boolean; // read off today's position changes; the transaction posts tomorrow
}

export interface PaperTradeSummary {
  realized: number;
  closed_trades: number;
  winners: number;
  win_rate: Num;
  fills: number;
  pnl: Num;
  by_strategy: Record<string, number>;
}

export interface Trades {
  trades: Trade[];
  mine: PnlSummary | null;
  paper: PaperTradeSummary | null;
  freshness: BrokerFreshness | null;
}

export interface Portfolio {
  synced: boolean;
  total_value?: number;
  cash?: number;
  cash_detail?: CashSummary;
  accounts: {
    source: string;
    account_name: string | null;
    account_number_masked: string | null;
    institution: string | null;
    cash: Num;
    total_value: Num;
    taken_at: string;
  }[];
  holdings: Holding[];
  by_sector?: Record<string, number>;
  by_asset_class?: Record<string, number>;
  concentration?: { largest_weight: Num; largest_symbol: string; effective_positions: Num };
  priced_share?: number;
  pnl?: PortfolioPnl;
  freshness?: BrokerFreshness;
}

export interface BrokerFreshness {
  synced_at: string | null; // when we last pulled from SnapTrade
  positions_as_of: string | null; // how old SnapTrade's copy of the positions was then
  transactions_through: string | null; // the last day of transactions it has
  problems?: string[]; // why the data may have stopped updating (a disabled connection, stale data)
}

export interface BrokerSyncState {
  running: boolean;
  refresh?: boolean;
  source?: string;
  started_at?: string;
  finished_at?: string;
  ok?: boolean;
  message?: string;
}

export type Stats = Record<string, Record<string, Num | string>>;
export type GrowthPoint = { date: string } & Record<string, Num | string>;

export interface Performance {
  holdings_backtest: { coverage: number; growth: GrowthPoint[]; stats: Stats } | null;
  account: { values: { date: string; value: Num }[]; growth: GrowthPoint[]; stats: Stats } | null;
}

export interface Idea {
  kind: "holding" | "portfolio" | "strategy" | "candidate";
  severity: "attention" | "consider" | "info";
  title: string;
  summary: string;
  rationale: string[];
  symbols: string[];
  score: Num;
  metrics: Record<string, Num>;
}

export interface Ideas {
  as_of: string | null;
  disclaimer: string;
  ideas: Idea[];
}

export interface StrategyView {
  universe: string;
  available: boolean;
  symbols?: string[];
  growth?: GrowthPoint[];
  stats?: Stats;
  exposure_now?: Num;
  trades?: { date: string; asset: string; weight_before: number; weight_after: number }[];
  signals?: Record<string, boolean>;
}

export interface Bars {
  symbol: string;
  bars: { date: string; open: number; high: number; low: number; close: number; adj_close: number; volume: number }[];
}

export interface OptionSummary {
  underlying: string;
  snapshot_at: string | null;
  spot?: Num;
  term: {
    expiration: string;
    dte: number;
    atm_strike: number;
    atm_iv: Num;
    expected_move: Num;
    expected_move_pct: Num;
    contracts: number;
    fit?: SmileFit | null;
  }[];
  smile: { strike: number; iv: Num; right: "C" | "P" }[];
  rate?: number;
  smile_expiration?: string | null;
  smile_fit?: { strike: number; iv: number }[];
  quote_arbitrage?: { vertical: number; butterfly: number } | null;
  skew?: { skew_25d: Num; butterfly_25d: Num; iv30: Num; iv90: Num; ratio: Num; shape: "contango" | "flat" | "backwardation" | null };
}

// tp_core.fundamentals.summary: from SEC filings, as known today
export interface Fundamentals {
  period_end: string | null;
  last_release: string | null;
  next_release_estimate: string | null;
  eps_last_quarter: Num;
  eps_ttm: Num;
  pe: Num;
  earnings_yield: Num;
  book_to_market: Num;
  gross_profitability: Num;
  roe: Num;
  market_cap: Num;
  sue: Num;
}

// tp_core.smile.ExpiryFit: SVI per expiration
export interface SmileFit {
  points: number;
  svi: { a: number; b: number; rho: number; m: number; sigma: number };
  rmse_iv: number;
  atm_iv: number;
  skew_25d: Num;
  butterfly_25d: Num;
  butterfly_arbitrage: boolean;
  calendar_arbitrage: boolean;
  k_min: number;
  k_max: number;
}

export interface PaperCheck {
  check: string;
  passed: boolean;
  detail: string;
}

export interface PaperProposal {
  id: string;
  strategy: string;
  session: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  approved_quantity: number | null;
  order_quantity: number;
  notional: number;
  reference_price: number;
  order_type: string;
  limit_price: number | null;
  reason: string;
  checks: PaperCheck[];
  status: string;
  note: string;
  decided_by: string | null;
  decided_at: string | null;
  filled_quantity: number;
  avg_fill_price: number | null;
  created_at: string;
  updated_at: string;
}

export interface PaperSleeve {
  strategy: string;
  approval: string;
  capital: number;
  equity: number;
  return: number | null;
  max_drawdown: number;
  sharpe: number | null;
  positions: Record<string, number>;
  trading_days: number;
  trades: number;
  slippage_bps: number | null;
  modeled_slippage_bps: number;
  gate: { days: [number, number]; trades: [number, number] };
  disabled: string | null;
  pending: number;
  since: string | null;
}

export interface PaperStatus {
  as_of: string;
  broker: string;
  broker_error: string | null;
  account: { number: string; status: string; equity: number; last_equity: number; cash: number } | null;
  book_capital: number;
  kill_switch: { reason: string; at: string } | null;
  reconciliation: { at: string; ok: boolean; breaks: Record<string, { ledger: number; broker: number }> } | null;
  sleeves: PaperSleeve[];
  pending: number;
  gate: { days: number; trades: number };
  bot: PaperBot | null;
}

export interface PaperBot {
  state: "running" | "stopped";
  alive: boolean;
  heartbeat: string | null;
  started_at: string | null;
  next: { task: string; session: string; due: string } | null;
  last: { task: string; session: string; outcome: string; at: string; message: string } | null;
}

export type PaperHistory = Record<string, { session: string; equity: number; growth?: Num }[]>;

export interface PaperEvent {
  id: number;
  at: string;
  kind: string;
  message: string;
}

// /api/positions (services/api/src/tp_api/positions.py)

export interface PositionNote {
  level: "info" | "warn" | "risk";
  title: string;
  text: string;
}

export interface ScenarioGrid {
  columns: string[]; // "Now", a date midway, "Expiry <date>"
  rows: { move: number; price: number; cells: { value: number; pnl: number }[] }[];
}

/** Per-position figures in dollars for the whole position; per-share ones as brokers quote. */
export interface OptionStats {
  right: "call" | "put";
  strike: number;
  expiration: string;
  expires_at: string;
  days_left: number;
  spot: Num;
  mark: Num;
  mark_source: "mid" | "last trade" | "broker" | "none";
  bid: Num;
  ask: Num;
  quote_at: string | null;
  cost: Num;
  value?: Num;
  pnl?: Num;
  pnl_pct?: Num;
  spread?: Num;
  spread_pct?: Num;
  exit_value?: Num;
  intrinsic?: Num;
  extrinsic?: Num;
  early_exercise_value?: Num; // puts: what exercising early adds per share (binomial tree)
  moneyness?: Num;
  in_the_money?: boolean;
  breakeven?: Num;
  breakeven_move_pct?: Num;
  iv?: Num;
  realized_vol?: Num;
  delta?: Num;
  gamma?: Num;
  theta?: Num;
  vega?: Num;
  delta_shares?: Num;
  theta_day?: Num;
  vega_point?: Num;
  prob_itm?: Num;
  prob_profit?: Num;
  expected_move?: Num;
  expected_move_pct?: Num;
  scenarios?: ScenarioGrid;
}

export interface StockStats {
  price: Num;
  prev_close?: Num;
  day_change?: Num;
  day_change_pct?: Num;
  returns?: Record<"1W" | "1M" | "3M" | "6M" | "1Y", Num>;
  ytd?: Num;
  high_52w?: Num;
  low_52w?: Num;
  from_high_pct?: Num;
  from_low_pct?: Num;
  vol_20d?: Num;
  vol_60d?: Num;
  max_drawdown_1y?: Num;
  ma20?: Num;
  ma50?: Num;
  ma200?: Num;
  vs_ma20?: Num;
  vs_ma50?: Num;
  vs_ma200?: Num;
  beta_1y?: Num;
}

interface PositionBase {
  symbol: string;
  label: string;
  description: string | null;
  underlying: string;
  quantity: number;
  price: Num;
  price_source: string;
  broker_price: Num;
  cost: Num; // per share
  market_value: Num;
  pnl: Num;
  pnl_pct: Num;
  day_pnl: Num;
  weight: Num;
  sector: string;
  asset_class: string;
  notes: PositionNote[];
  actions?: HoldingAction[]; // choices priced from the latest option chain
}

export interface StockPosition extends PositionBase {
  kind: string; // stock, etf, mutualfund, ...
  stats: StockStats;
}

export interface OptionPositionRow extends PositionBase {
  kind: "option";
  stats: OptionStats;
  underlying_stats: Partial<StockStats>;
}

export type PositionRow = StockPosition | OptionPositionRow;

export function isOption(p: PositionRow): p is OptionPositionRow {
  return p.kind === "option";
}

export interface CashSummary {
  cash: number; // what's really there: the broker's total less the holdings
  reported_cash: number; // the broker's cash balance
  unsettled: number; // reported - cash: paying for trades that haven't settled
  buying_power: Num;
  holdings_value: number;
  total_value: number;
}

export interface Positions {
  synced: boolean;
  now?: string;
  freshness?: BrokerFreshness;
  total_value?: number;
  cash?: CashSummary;
  market_data?: { source: string | null; error: string | null; option_quotes: number; options: number };
  risk_free_rate?: number;
  totals?: {
    value: number;
    pnl: number;
    day_pnl: number;
    options_value: number;
    options_pnl: number;
    theta_day: number;
    expiring_within_7d: number;
  };
  exposure?: {
    underlying: string;
    shares: number;
    option_delta_shares: number;
    net_shares: number;
    net_value: Num;
    price: Num;
    options: number;
  }[];
  positions: PositionRow[];
  ask?: { available: boolean; model: string };
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface AskReply {
  answer: string;
  model: string;
  stop_reason: string | null;
  sources: { url: string; title: string }[];
  searches: number;
  usage: { input_tokens: number; output_tokens: number };
  note: string | null;
  as_of: string;
}

// /api/research (services/api/src/tp_api/research.py, strategies/.../options_research.py)

export interface ContractPick {
  contract: string;
  label: string;
  right: "call" | "put";
  strike: number;
  expiration: string;
  dte: number;
  bid: number;
  ask: number;
  mid: number;
  spread_pct: number;
  open_interest: Num;
  price: number; // per share: the ask to buy, the bid to sell
  cost: number; // per contract
  iv: Num;
  delta: number;
  theta_day: number; // per contract
  vega: number;
  intrinsic: number;
  extrinsic: number;
  breakeven: number;
  breakeven_move_pct: number;
  prob_itm: Num;
  prob_profit: Num;
  expected_move_pct: Num;
  leverage: Num;
  scenarios?: ScenarioGrid | null;
  // LEAPS as a stand-in for 100 shares
  shares_equivalent?: number;
  stock_cost?: number;
  capital_freed?: number;
  carry_per_year_pct?: Num;
  breakeven_cagr?: Num;
  // covered call
  contracts?: number;
  premium?: number;
  yield_pct?: number;
  annualized_pct?: number;
  upside_to_strike_pct?: number;
  prob_assigned?: Num;
  // cash-secured put
  cash_required?: number;
  net_entry?: number;
  net_entry_discount_pct?: number;
}

/** Selling a vertical: the short leg nearer the money, bought back further out. */
export interface CreditSpreadPick {
  kind: "put credit spread" | "call credit spread";
  short: ContractPick;
  long: ContractPick;
  credit: number;
  premium: number;
  width: number;
  max_loss: number;
  return_on_risk: number;
  breakeven: number;
  breakeven_move_pct: number;
  prob_profit: Num;
  theta_day: number;
  dte: number;
}

export interface IronCondorPick {
  kind: "iron condor";
  put_spread: CreditSpreadPick;
  call_spread: CreditSpreadPick;
  credit: number;
  premium: number;
  max_loss: number;
  return_on_risk: number;
  lower_breakeven: number;
  upper_breakeven: number;
  range_pct: [number, number];
  prob_profit: Num;
  theta_day: number;
  dte: number;
}

export interface CalendarPick {
  kind: "calendar";
  front: ContractPick;
  back: ContractPick;
  strike: number;
  debit: number;
  cost: number;
  max_profit_est: number;
  profit_range: [Num, Num];
  prob_profit: Num;
  theta_day: number;
  vega: number;
  front_iv: number;
  back_iv: number;
}

export interface SpreadPick {
  long: ContractPick;
  short: ContractPick;
  debit: number;
  cost: number;
  max_gain: number;
  reward_risk: number;
  breakeven: number;
  breakeven_move_pct: number;
  prob_profit: Num;
  theta_day: number;
}

export type Pick = ContractPick | SpreadPick | CreditSpreadPick | IronCondorPick | CalendarPick;

/** A debit spread (the structures with a ``kind`` are the premium-selling and time spreads). */
export function isSpread(p: Pick): p is SpreadPick {
  return "long" in p && !("kind" in p);
}

export function isCreditSpread(p: Pick): p is CreditSpreadPick {
  return "kind" in p && (p.kind === "put credit spread" || p.kind === "call credit spread");
}

export function isCondor(p: Pick): p is IronCondorPick {
  return "kind" in p && p.kind === "iron condor";
}

export function isCalendar(p: Pick): p is CalendarPick {
  return "kind" in p && p.kind === "calendar";
}

export interface VolContext {
  iv30: Num;
  hv20: Num;
  iv_hv: Num;
  rv_forecast?: Num; // the next month's volatility, forecast (HAR-RV)
  iv_rv?: Num; // implied over that forecast: what "expensive" is judged by

  rank: Num;
  percentile: Num;
  history_days: number;
  iv_low?: Num;
  iv_high?: Num;
  event: { by: string; after: string | null; iv: number; iv_after: number } | null;
  rich: boolean;
  as_of: string | null;
  skew_25d?: Num; // 25-delta call IV minus put IV (negative: puts dearer)
  butterfly_25d?: Num;
  smile_arbitrage?: boolean | null;
  term_ratio?: Num; // 90-day over 30-day IV
  term_shape?: "contango" | "flat" | "backwardation" | null;
}

export interface Setup {
  direction: "bullish" | "bearish" | "neutral";
  tags: string[];
  rs_3m: Num;
}

export interface ResearchIdea {
  symbol: string;
  kind: "swing" | "leaps" | "bearish" | "income";
  score: number;
  spot: Num;
  setup: Setup;
  stock: StockStats;
  vol: VolContext;
  pick: Pick | null;
  alternative: Pick | null;
  why: string[];
  risks: string[];
  held: string | null;
}

export interface Research {
  as_of: string;
  chains_as_of: string | null;
  scanned: number;
  with_chains: number;
  without_chains: string[];
  iv_history_days: number;
  swing: ResearchIdea[];
  leaps: ResearchIdea[];
  bearish: ResearchIdea[];
  income: ResearchIdea[];
  errors?: Record<string, string>; // symbols the scan skipped, with why
  disclaimer: string;
}

export interface SymbolResearch {
  symbol: string;
  spot: Num;
  stock: StockStats;
  setup: Setup;
  vol: VolContext;
  picks: Partial<Record<"swing_call" | "swing_put" | "leaps_call" | "cash_secured_put", ContractPick>> &
    Partial<Record<"call_spread" | "put_spread", SpreadPick>> &
    Partial<Record<"put_credit_spread" | "call_credit_spread", CreditSpreadPick>> & {
      iron_condor?: IronCondorPick;
      calendar?: CalendarPick;
    };
  chain_age_days: Num;
  fundamentals?: Fundamentals | null;
  held: string | null;
  disclaimer: string;
  ask?: { available: boolean; model: string };
}

export interface HoldingAction {
  level: "attention" | "consider" | "info";
  title: string;
  text: string;
  trade: ContractPick | null;
}

export interface FetchState {
  running: boolean;
  symbol?: string;
  ok?: boolean;
  message?: string;
  started_at?: string;
  finished_at?: string;
}

export interface DataMap {
  root: string;
  datasets: {
    name: string;
    path: string;
    also?: string;
    writer: string;
    used_by: string;
    files: number;
    bytes: number;
    updated_at: string | null;
  }[];
}

// /api/strategies/validation (strategies/src/tp_strategies/validation.py)

export interface PeriodStats {
  start?: string;
  end?: string;
  sessions: number;
  total_return?: Num;
  cagr?: Num;
  ann_volatility?: Num;
  sharpe?: Num;
  max_drawdown?: Num;
  benchmark?: { cagr: Num; sharpe: Num; max_drawdown: Num; total_return: Num };
}

export interface GateCheck {
  name: string;
  passed: boolean | null;
  detail: string;
}

export interface RegimeRow {
  label: string;
  days: number;
  share: number;
  ann_return: Num;
  ann_volatility: Num;
  sharpe: Num;
  benchmark_ann_return?: Num;
}

export interface ValidationReport {
  run_id: string;
  created_at: string;
  strategy: string;
  title: string;
  params: Record<string, unknown>;
  full: PeriodStats;
  periods: { train: PeriodStats; validation: PeriodStats; test: PeriodStats };
  walk_forward: {
    windows: { start: string; end: string; params: Record<string, unknown>; train_sharpe: Num; test_return: Num }[];
    sessions?: number;
    sharpe?: Num;
    cagr?: Num;
    max_drawdown?: Num;
    efficiency?: Num;
  };
  deflated: { sharpe: Num; trials: number; threshold: Num; probability: Num; excess: Num };
  stress: { name: string; start: string; end: string; passed: boolean | null; note?: string; return?: Num; max_drawdown?: Num; benchmark_return?: Num; benchmark_max_drawdown?: Num }[];
  stability: {
    variants: { params: Record<string, unknown>; sharpe: Num; cagr: Num; max_drawdown: Num; base: boolean }[];
    base_sharpe: Num;
    worst_neighbor_sharpe: Num;
    passed: boolean | null;
  };
  monte_carlo: { paths?: number; cagr_p5?: number; cagr_p50?: number; cagr_p95?: number; max_drawdown_p5?: number; max_drawdown_p50?: number; sharpe_p5?: number; prob_loss?: number };
  regimes: Partial<Record<"trend" | "volatility" | "breadth" | "rates" | "curve", RegimeRow[]>>;
  test_touches: number;
  gate: { passed: boolean; checks: GateCheck[] };
}

export interface StrategyValidation {
  name: string;
  title: string;
  spec: string;
  on_paper: boolean;
  report: ValidationReport | null;
}

export interface Validation {
  strategies: StrategyValidation[];
  job: { running: boolean; names?: string[]; done?: string[]; current?: string | null; ok?: boolean; message?: string; finished_at?: string };
  periods: { train_end: string; validation_end: string };
  gate: Record<string, number>;
}

export interface MarketRegime {
  current: {
    date: string;
    trend: "up" | "down" | null;
    volatility: "calm" | "normal" | "stressed" | null;
    breadth: "broad" | "mixed" | "narrow" | null;
    spy_vs_ma200: Num;
    realized_vol: Num;
    breadth_pct: Num;
    rates?: "rising" | "steady" | "falling" | null;
    curve?: "normal" | "flat" | "inverted" | null;
    volatility_source?: "vix" | "realized";
    vix?: Num;
    vix_ratio?: Num;
    rate_10y?: Num; // percent
    rate_3m?: Num;
    curve_10y_3m?: Num; // percentage points
    summary: string;
  } | null;
  history: { date: string; trend: string | null; volatility: string | null; breadth: string | null; rates?: string | null; curve?: string | null }[];
}

// /api/positions/risk (libs/core/src/tp_core/portfolio_risk.py)

export interface PortfolioRisk {
  synced: boolean;
  exposure?: number;
  gross_exposure?: number;
  coverage?: Num;
  value?: number;
  sessions?: number;
  beta?: Num;
  beta_dollars?: Num;
  volatility?: Num;
  var?: { historical_95: number; historical_99: number; parametric_95: number; parametric_99: number; expected_shortfall_95: Num };
  worst_days?: { date: string; pnl: number }[];
  betas?: Record<string, number>;
  correlation?: { symbols: string[]; matrix: (number | null)[][]; average: Num; most_correlated: { a: string; b: string; correlation: number }[] } | null;
  greeks?: { delta_dollars: number; gamma_1pct: number; theta_day: number; vega_point: number };
  scenarios?: { vol_shifts: number[]; rows: { move: number; pnl: number[] }[] };
}

// /api/trades/stats (libs/core/src/tp_core/trade_stats.py): how you trade, from round trips

export interface TradeGroup {
  trades: number;
  win_rate?: Num;
  median_return?: Num;
  avg_return?: Num;
  weighted_return?: Num; // total P&L over total cost
  avg_win?: Num;
  avg_loss?: Num;
  total_pnl?: number;
  profit_factor?: Num;
  median_days?: Num;
  avg_days?: Num;
  median_days_winners?: Num;
  median_days_losers?: Num;
  avg_size?: Num;
  expired?: number;
}

export interface TripView {
  label: string;
  kind: "call" | "put" | "shares";
  side: "long" | "short";
  opened: string;
  closed: string;
  days: number;
  pnl: number;
  return_pct: Num;
  exit: string;
}

export type TradeKind = "all" | "calls" | "puts" | "shares" | "options_sold";

export interface TradeStats {
  synced: boolean;
  since?: string | null;
  by_kind?: Record<TradeKind, TradeGroup>;
  holding?: Partial<Record<TradeKind, { bucket: string; trades: number }[]>>;
  monthly?: { month: string; pnl: number; trades: number }[];
  best?: TripView[];
  worst?: TripView[];
  open?: { positions: number; median_age_days: Num; oldest_days: Num };
  unknown_cost?: number;
  observations?: string[];
}
