import { useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { money, num, pct, signedMoney } from "../format";
import type { PortfolioRisk } from "../types";

/** How the whole book moves: beta, VaR, correlation, option Greeks, and a market-move by
 * volatility scenario grid with the options repriced. */
export default function RiskView() {
  const data = useApi<PortfolioRisk>("/api/positions/risk", 120_000);
  const r = data.data;
  if (data.loading && !r) return <Loading />;
  if (!r?.synced) return <ErrorBanner error={data.error} />;
  if (!r.var) {
    return (
      <Card title="Risk">
        <Empty>Needs daily bars for your holdings and SPY: run <code>uv run tp-data bars daily</code>.</Empty>
      </Card>
    );
  }
  const v = r.var;
  const g = r.greeks;
  return (
    <div className="grid">
      <div className="grid tiles">
        <Stat label="Beta to the S&P 500" value={num(r.beta, 2)} sub={`the book moves ${signedMoney((r.beta_dollars ?? 0) / 100)} per 1% S&P move`} />
        <Stat label="Volatility" value={pct(r.volatility, 1)} sub={`a year, from ${r.sessions} days of today's exposures`} />
        <Stat label="1-day VaR (95%)" value={money(v.historical_95)} sub={`1 day in 20 loses more · 99%: ${money(v.historical_99)}`} />
        <Stat label="Beyond the VaR" value={money(v.expected_shortfall_95)} sub="average loss on those worst 5% of days" />
      </div>
      {r.coverage != null && r.coverage < 0.98 && (
        <div className="banner">
          These figures cover {pct(r.coverage, 0)} of your exposure: holdings without daily bars (mutual funds, symbols not ingested yet) are left out.
        </div>
      )}
      <div className="grid cols-2 top">
        {r.scenarios && (
          <Card title="If the market moved" hint="P&L for an S&P 500 move (each name by its beta), with implied volatility down, flat or up 5 points">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>S&P 500</th>
                    {r.scenarios.vol_shifts.map((s) => (
                      <th key={s} className="num">
                        IV {s === 0 ? "unchanged" : `${s > 0 ? "+" : "−"}${Math.round(Math.abs(s * 100))} pts`}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {r.scenarios.rows.map((row) => (
                    <tr key={row.move} className={row.move === 0 ? "selected" : undefined}>
                      <td>{row.move === 0 ? "unchanged" : pct(row.move, 0, true)}</td>
                      {row.pnl.map((p, i) => (
                        <td key={i} className="num">
                          <Delta value={p} kind="money" />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
        <Card title="Value at Risk" hint="one day, today's exposures">
          <table>
            <tbody>
              <tr>
                <td>Historical, 95%</td>
                <td className="num">{money(v.historical_95)}</td>
                <td className="muted small">the loss exceeded on 1 day in 20 over the last year</td>
              </tr>
              <tr>
                <td>Historical, 99%</td>
                <td className="num">{money(v.historical_99)}</td>
                <td className="muted small">1 day in 100</td>
              </tr>
              <tr>
                <td>Parametric, 95% / 99%</td>
                <td className="num">
                  {money(v.parametric_95)} / {money(v.parametric_99)}
                </td>
                <td className="muted small">from volatility, assuming normal returns (understates fat tails)</td>
              </tr>
            </tbody>
          </table>
          {r.worst_days && r.worst_days.length > 0 && (
            <p className="small muted">
              Worst days for today's book: {r.worst_days.map((d) => `${d.date} ${signedMoney(d.pnl)}`).join(" · ")}
            </p>
          )}
        </Card>
        {g && (
          <Card title="Option Greeks, all contracts" hint="in dollars">
            <div className="metrics">
              <div className="metric">
                <div className="label">Delta</div>
                <div className="value">{money(g.delta_dollars)}</div>
                <div className="sub">acts like this much stock</div>
              </div>
              <div className="metric">
                <div className="label">Gamma</div>
                <div className="value">{signedMoney(g.gamma_1pct)}</div>
                <div className="sub">delta change for a 1% move</div>
              </div>
              <div className="metric">
                <div className="label">Theta</div>
                <div className="value">{signedMoney(g.theta_day)}/day</div>
              </div>
              <div className="metric">
                <div className="label">Vega</div>
                <div className="value">{signedMoney(g.vega_point)}</div>
                <div className="sub">per volatility point</div>
              </div>
            </div>
          </Card>
        )}
        {r.correlation && (
          <Card title="Correlation" hint={`largest exposures, one year of daily returns · average ${num(r.correlation.average, 2)}`}>
            {r.correlation.most_correlated.length > 0 && (
              <p className="small">
                Most correlated:{" "}
                {r.correlation.most_correlated.map((p) => `${p.a}/${p.b} ${num(p.correlation, 2)}`).join(" · ")}
              </p>
            )}
            <div className="table-wrap">
              <table className="corr">
                <thead>
                  <tr>
                    <th />
                    {r.correlation.symbols.map((s) => (
                      <th key={s} className="num">
                        {s}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {r.correlation.symbols.map((s, i) => (
                    <tr key={s}>
                      <th>{s}</th>
                      {r.correlation!.matrix[i]!.map((c, j) => (
                        <td key={j} className="num" style={{ background: c != null && i !== j ? `color-mix(in srgb, var(--series-2) ${Math.round(Math.max(c, 0) * 45)}%, transparent)` : undefined }}>
                          {i === j ? "" : num(c, 2)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
