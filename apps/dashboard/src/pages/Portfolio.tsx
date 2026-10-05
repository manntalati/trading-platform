import { useMemo, useState } from "react";
import { useApi } from "../api";
import { useThemeColors, GrowthChart } from "../components/charts";
import { BarList, Card, Delta, Empty, ErrorBanner, Loading, Stat, StatsTable } from "../components/ui";
import { money, pct, qty } from "../format";
import type { LiveState } from "../live";
import type { Performance, Portfolio as PortfolioData } from "../types";

const RANGES = [
  { label: "6M", days: 182 },
  { label: "1Y", days: 365 },
  { label: "3Y", days: 1095 },
];

export default function Portfolio({ live }: { live: LiveState }) {
  const [days, setDays] = useState(365);
  const portfolio = useApi<PortfolioData>("/api/portfolio", 60_000);
  const perf = useApi<Performance>(`/api/portfolio/performance?days=${days}`, 300_000);
  const theme = useThemeColors();

  const holdings = useMemo(() => {
    return (portfolio.data?.holdings ?? []).map((h) => {
      const q = live.quotes[h.symbol];
      const livePrice = q?.price ?? null;
      const liveValue = livePrice != null && h.quantity != null ? livePrice * h.quantity : h.market_value;
      return { ...h, livePrice, liveValue, change: q?.change_pct ?? null };
    });
  }, [portfolio.data, live.quotes]);

  if (portfolio.loading) return <Loading />;
  const p = portfolio.data;
  if (!p?.synced) {
    return (
      <Card title="Portfolio">
        <ErrorBanner error={portfolio.error} />
        <Empty>
          No brokerage synced yet. Link Fidelity (<code>uv run tp-broker link</code>), then{" "}
          <code>uv run tp-broker sync</code>. Or try <code>--source fake</code>.
        </Empty>
      </Card>
    );
  }
  const bt = perf.data?.holdings_backtest;
  const acct = perf.data?.account;
  const btSeries = bt ? Object.keys(bt.stats) : [];
  const liveTotal = live.portfolio?.value ?? p.total_value ?? null;

  return (
    <div className="grid">
      <div className="grid tiles">
        <Stat label="Total value" value={money(liveTotal)} sub={live.portfolio ? <><Delta value={live.portfolio.day_pnl} kind="money" /> today</> : "Last sync"} />
        <Stat label="Cash" value={money(p.cash)} sub={pct((p.cash ?? 0) / (p.total_value || 1), 1) + " of portfolio"} />
        <Stat
          label="Largest position"
          value={p.concentration?.largest_symbol || "—"}
          sub={`${pct(p.concentration?.largest_weight)} · ~${p.concentration?.effective_positions?.toFixed(1) ?? "—"} effective positions`}
        />
        <Stat label="Price-analysed" value={pct(p.priced_share, 0)} sub="Share with market data (funds & options excluded)" />
      </div>

      <Card title="Performance vs benchmarks" hint="current holdings, held constant">
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
            <GrowthChart data={bt.growth} series={btSeries} colors={theme.series} />
            <StatsTable stats={bt.stats} colors={theme.series} />
          </>
        ) : (
          <Empty>{perf.loading ? "Loading…" : "Needs daily bars for your holdings: run tp-data bars daily."}</Empty>
        )}
      </Card>

      {acct && acct.growth.length > 1 && (
        <Card title="Account performance" hint="time-weighted, deposits and withdrawals removed">
          <GrowthChart data={acct.growth} series={Object.keys(acct.stats)} colors={theme.series} />
          <StatsTable stats={acct.stats} colors={theme.series} />
        </Card>
      )}

      <div className="grid cols-2">
        <Card title="By sector" hint="30% cap">
          <BarList data={p.by_sector ?? {}} cap={0.3} capLabel="Above the 30% sector cap" />
        </Card>
        <Card title="By asset class">
          <BarList data={p.by_asset_class ?? {}} />
        </Card>
      </div>

      <Card title="Holdings" hint={`${holdings.length} positions`}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Kind</th>
                <th className="num">Qty</th>
                <th className="num">Price</th>
                <th className="num">Today</th>
                <th className="num">Value</th>
                <th className="num">Weight</th>
                <th className="num">Unrealized</th>
                <th>Sector</th>
              </tr>
            </thead>
            <tbody>
              {holdings.map((h) => (
                <tr key={h.symbol}>
                  <td title={h.description ?? undefined}>
                    <strong>{h.symbol}</strong>
                  </td>
                  <td className="muted">{h.kind}</td>
                  <td className="num">{qty(h.quantity)}</td>
                  <td className="num">{money(h.livePrice ?? h.price, true)}</td>
                  <td className="num">{h.change != null ? <Delta value={h.change} /> : <span className="muted">—</span>}</td>
                  <td className="num">{money(h.liveValue)}</td>
                  <td className="num">{pct(h.weight)}</td>
                  <td className="num">
                    <Delta value={h.unrealized_pnl} kind="money" />
                  </td>
                  <td className="muted">{h.sector}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
