import { useState } from "react";
import { useApi } from "../api";
import { FidelityFreshness } from "../components/broker";
import { GrowthChart, useThemeColors } from "../components/charts";
import { BarList, Card, Delta, Empty, ErrorBanner, go, Loading, Stat, StatsTable, SubNav } from "../components/ui";
import { money, pct, signedMoney } from "../format";
import type { LiveState } from "../live";
import type { Performance, Portfolio as PortfolioData, Positions as PositionsData } from "../types";
import PositionsView, { CashNote, ExposureCard } from "./Positions";
import RiskView from "./Risk";

const VIEWS = [
  { key: "positions", label: "Positions" },
  { key: "allocation", label: "Allocation" },
  { key: "risk", label: "Risk" },
  { key: "performance", label: "Performance" },
];

/** Everything you own, in one place: the account's numbers up top, then every position (with
 * what you could do about it), how the money is spread, and how it has performed. */
export default function Portfolio({ live, sub, detail }: { live: LiveState; sub: string | null; detail: string | null }) {
  const current = VIEWS.some((v) => v.key === sub) ? sub! : "positions";
  const portfolio = useApi<PortfolioData>("/api/portfolio", 60_000);
  const positions = useApi<PositionsData>("/api/positions", 30_000);
  const p = portfolio.data;
  const pos = positions.data;

  if (portfolio.loading && !p) return <Loading />;
  if (!p?.synced) {
    return (
      <Card title="Portfolio">
        <ErrorBanner error={portfolio.error} />
        <Empty>
          No brokerage synced yet. Link Fidelity (<code>uv run tp-broker link</code>), then <code>uv run tp-broker sync</code>. Or try{" "}
          <code>--source fake</code> for a demo.
        </Empty>
      </Card>
    );
  }
  const pnl = p.pnl;
  const total = live.portfolio?.value ?? p.total_value ?? null;
  const t = pos?.totals;
  const options = pos?.positions.filter((x) => x.kind === "option").length ?? 0;
  const reload = () => {
    portfolio.reload();
    positions.reload();
  };
  return (
    <div className="grid">
      <ErrorBanner error={portfolio.error ?? positions.error} />
      <FidelityFreshness freshness={p.freshness} onSynced={reload} />
      {pos?.cash && <CashNote cash={pos.cash} />}
      <div className="grid tiles">
        <Stat
          label="Account value"
          value={money(total)}
          sub={live.portfolio ? <><Delta value={live.portfolio.day_pnl} kind="money" /> today, live</> : "at the last sync"}
        />
        <Stat
          label="Total P&L"
          value={pnl ? <Delta value={pnl.total} kind="money" /> : "—"}
          sub={pnl ? <><Delta value={pnl.unrealized} kind="money" /> open · <Delta value={pnl.realized} kind="money" /> closed</> : null}
        />
        <Stat label="Cash" value={money(p.cash)} sub={`${pct((p.cash ?? 0) / (p.total_value || 1), 1)} of the account`} />
        {options > 0 && (
          <Stat
            label="Options time decay"
            value={t ? signedMoney(t.theta_day) : "—"}
            sub={`a day across ${options} contracts · ${t?.expiring_within_7d ?? 0} expire within 7 days`}
          />
        )}
      </div>
      <SubNav base="portfolio" current={current} items={VIEWS} />
      {current === "positions" &&
        (pos ? (
          pos.synced ? (
            <PositionsView data={pos} selected={detail} onSelect={(s) => go(s ? `portfolio/positions/${encodeURIComponent(s)}` : "portfolio/positions")} />
          ) : (
            <Empty>No positions synced.</Empty>
          )
        ) : (
          <Loading />
        ))}
      {current === "allocation" && <Allocation p={p} positions={pos} />}
      {current === "risk" && <RiskView />}
      {current === "performance" && <PerformanceView />}
    </div>
  );
}

function Allocation({ p, positions }: { p: PortfolioData; positions: PositionsData | null }) {
  return (
    <div className="grid">
      <div className="grid tiles">
        <Stat
          label="Largest position"
          value={p.concentration?.largest_symbol || "—"}
          sub={`${pct(p.concentration?.largest_weight)} of the account`}
        />
        <Stat
          label="Effective positions"
          value={p.concentration?.effective_positions?.toFixed(1) ?? "—"}
          sub="how many equal-sized positions the spread is worth"
        />
      </div>
      <div className="grid cols-2">
        <Card title="By sector" hint="30% cap in the risk plan">
          <BarList data={p.by_sector ?? {}} cap={0.3} capLabel="Above the 30% sector cap" />
        </Card>
        <Card title="By asset class">
          <BarList data={p.by_asset_class ?? {}} />
        </Card>
      </div>
      {positions?.exposure && positions.exposure.length > 0 ? (
        <ExposureCard exposure={positions.exposure} />
      ) : null}
    </div>
  );
}

const RANGES = [
  { label: "6M", days: 182 },
  { label: "1Y", days: 365 },
  { label: "3Y", days: 1095 },
];

function PerformanceView() {
  const [days, setDays] = useState(365);
  const perf = useApi<Performance>(`/api/portfolio/performance?days=${days}`, 300_000);
  const theme = useThemeColors();
  const bt = perf.data?.holdings_backtest;
  const acct = perf.data?.account;
  return (
    <div className="grid">
      {acct && acct.growth.length > 1 && (
        <Card title="Your account" hint="time-weighted: deposits and withdrawals removed">
          <GrowthChart data={acct.growth} series={Object.keys(acct.stats)} colors={theme.series} />
          <StatsTable stats={acct.stats} colors={theme.series} />
        </Card>
      )}
      <Card title="Today's mix vs benchmarks" hint="current holdings held constant over past prices">
        <div className="toolbar">
          {RANGES.map((r) => (
            <button key={r.label} aria-pressed={days === r.days} onClick={() => setDays(r.days)}>
              {r.label}
            </button>
          ))}
          {bt && <span className="muted small">Covers {pct(bt.coverage, 0)} of the portfolio. Describes today's mix, not your past trades.</span>}
        </div>
        <ErrorBanner error={perf.error} />
        {bt ? (
          <>
            <GrowthChart data={bt.growth} series={Object.keys(bt.stats)} colors={theme.series} />
            <StatsTable stats={bt.stats} colors={theme.series} />
          </>
        ) : (
          <Empty>{perf.loading ? "Loading…" : "Needs daily bars for your holdings: run tp-data bars daily."}</Empty>
        )}
      </Card>
    </div>
  );
}
