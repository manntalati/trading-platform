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
  term: { expiration: string; dte: number; atm_strike: number; atm_iv: Num; expected_move: Num; expected_move_pct: Num; contracts: number }[];
  smile: { strike: number; iv: Num; right: "C" | "P" }[];
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
