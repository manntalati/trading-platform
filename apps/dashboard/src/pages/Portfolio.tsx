import { useMemo, useState } from "react";
import { useApi } from "../api";
import { useThemeColors, GrowthChart } from "../components/charts";
import { FidelityFreshness } from "../components/broker";
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
    return (portfolio.data?.holdings ?? [])
      .filter((h) => h.kind !== "option")
      .map((h) => {
        const q = live.quotes[h.symbol];
        const livePrice = q?.price ?? null;
        const liveValue = livePrice != null && h.quantity != null ? livePrice * h.quantity : h.market_value;
        const cost = h.cost_basis_per_unit != null && h.quantity != null ? h.cost_basis_per_unit * h.quantity : null;
        const liveUnrealized = cost != null && liveValue != null && h.kind !== "cash" ? liveValue - cost : h.unrealized_pnl;
        const liveUnrealizedPct = cost ? (liveUnrealized ?? 0) / Math.abs(cost) : h.unrealized_pct;
        return { ...h, livePrice, liveValue, liveUnrealized, liveUnrealizedPct, change: q?.change_pct ?? null };
      });
  }, [portfolio.data, live.quotes]);
  const options = useMemo(() => {
    return (portfolio.data?.holdings ?? [])
      .filter((h) => h.kind === "option")
      .map((h) => {
        const spot = live.quotes[h.underlying ?? ""]?.price ?? h.underlying_price ?? null;
        const strike = h.strike ?? null;
        const moneyness = spot != null && strike ? (spot / strike - 1) * (h.right === "C" ? 1 : -1) : h.moneyness ?? null;
        return { ...h, spot, moneyness };
      })
      .sort((a, b) => (a.days_to_expiry ?? 1e9) - (b.days_to_expiry ?? 1e9));
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
  const pnl = p.pnl;

  return (
    <div className="grid">
      <FidelityFreshness
        freshness={p.freshness}
        onSynced={() => {
          portfolio.reload();
          perf.reload();
        }}
      />
      <div className="grid tiles">
        <Stat label="Total value" value={money(liveTotal)} sub={live.portfolio ? <><Delta value={live.portfolio.day_pnl} kind="money" /> today (stocks and ETFs, live)</> : "Last sync"} />
        <Stat
          label="Total P&L"
          value={pnl ? <Delta value={pnl.total} kind="money" /> : "—"}
          sub={pnl ? `open positions + closed trades + dividends${pnl.since ? ` since ${pnl.since}` : ""}` : null}
        />
        <Stat
          label="Unrealized P&L"
          value={pnl ? <Delta value={pnl.unrealized} kind="money" /> : "—"}
          sub={pnl ? <><Delta value={pnl.unrealized_pct} /> on what the open positions cost (last sync)</> : null}
        />
        <Stat
          label="Realized P&L"
          value={pnl ? <Delta value={pnl.realized} kind="money" /> : "—"}
          sub={
            pnl ? (
              <>
                <Delta value={pnl.realized_ytd} kind="money" /> this year · {pnl.closed_trades} closed
                {pnl.win_rate != null ? `, ${pct(pnl.win_rate, 0)} winners` : ""} · <a href="#/trades">trades</a>
              </>
            ) : null
          }
        />
        <Stat label="Cash" value={money(p.cash)} sub={pct((p.cash ?? 0) / (p.total_value || 1), 1) + " of portfolio"} />
        <Stat
          label="Largest position"
          value={p.concentration?.largest_symbol || "—"}
          sub={`${pct(p.concentration?.largest_weight)} · ~${p.concentration?.effective_positions?.toFixed(1) ?? "—"} effective positions`}
        />
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

      {options.length > 0 && <OptionsCard options={options} />}

      <Card title="Holdings" hint={`${holdings.length} positions${options.length ? `, plus ${options.length} options above` : ""}`}>
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
                    <Delta value={h.liveUnrealized} kind="money" />
                    <div className="small">
                      <Delta value={h.liveUnrealizedPct} digits={1} />
                    </div>
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

export type OptionRow = PortfolioData["holdings"][number] & { spot: number | null; moneyness: number | null };

/** Option positions the way the broker shows them: contracts, premium per share, and how far the
 * underlying is from the strike and from breakeven. */
export function OptionsCard({ options }: { options: OptionRow[] }) {
  const value = options.reduce((sum, o) => sum + (o.market_value ?? 0), 0);
  const unrealized = options.reduce((sum, o) => sum + (o.unrealized_pnl ?? 0), 0);
  return (
    <Card
      title="Options"
      hint={
        <>
          {options.length} positions · {money(value)} · <Delta value={unrealized} kind="money" /> unrealized · premiums from the last brokerage sync, underlyings live
        </>
      }
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Contract</th>
              <th className="num">Contracts</th>
              <th className="num">Premium</th>
              <th className="num">Cost</th>
              <th className="num">Value</th>
              <th className="num">Unrealized</th>
              <th className="num">Underlying</th>
              <th className="num">Breakeven</th>
              <th>Expires</th>
            </tr>
          </thead>
          <tbody>
            {options.map((o) => {
              const dte = o.days_to_expiry ?? null;
              const soon = dte != null && dte <= 7;
              return (
                <tr key={o.symbol}>
                  <td title={o.symbol}>
                    <strong>{o.label}</strong>
                  </td>
                  <td className="num">{qty(o.quantity)}</td>
                  <td className="num">{money(o.premium, true)}</td>
                  <td className="num muted">{money(o.cost_premium, true)}</td>
                  <td className="num">{money(o.market_value)}</td>
                  <td className="num">
                    <Delta value={o.unrealized_pnl} kind="money" />
                    <div className="small">
                      <Delta value={o.unrealized_pct} digits={1} />
                    </div>
                  </td>
                  <td className="num">
                    {money(o.spot, true)}
                    <div className="small muted">
                      {o.moneyness == null ? "—" : `${o.moneyness >= 0 ? "in" : "out of"} the money by ${pct(Math.abs(o.moneyness), 1)}`}
                    </div>
                  </td>
                  <td className="num">
                    {money(o.breakeven, true)}
                    <div className="small muted">{breakevenNote(o)}</div>
                  </td>
                  <td>
                    {o.expiration ?? "—"}
                    <div className="small">
                      {dte == null ? null : dte < 0 ? (
                        <span className="muted">expired</span>
                      ) : soon ? (
                        <span>
                          <span className="dot warning" aria-hidden="true" /> {dte === 0 ? "today" : `in ${dte} day${dte === 1 ? "" : "s"}`}
                        </span>
                      ) : (
                        <span className="muted">in {dte} days</span>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** What the underlying has to do by expiry for the contract to pay back its cost. */
export function breakevenNote(o: { right?: "C" | "P" | null; spot: number | null; breakeven?: number | null }): string {
  if (o.spot == null || o.breakeven == null || !o.spot) return "";
  const move = o.breakeven / o.spot - 1;
  if (o.right === "C") return move <= 0 ? "above breakeven" : `needs ${pct(move, 1, true)}`;
  return move >= 0 ? "below breakeven" : `needs ${pct(move, 1, true)}`;
}
