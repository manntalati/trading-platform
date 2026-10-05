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
}

export interface Holding {
  symbol: string;
  description: string | null;
  kind: string;
  quantity: Num;
  price: Num;
  market_value: Num;
  cost_basis_per_unit: Num;
  unrealized_pnl: Num;
  weight: Num;
  sector: string;
  asset_class: string;
  priced: boolean;
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
