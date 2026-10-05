import { useState } from "react";
import { useApi } from "../api";
import { GrowthChart, useThemeColors } from "../components/charts";
import { Card, Empty, ErrorBanner, Loading, StatsTable } from "../components/ui";
import { pct } from "../format";
import type { StrategyView } from "../types";

export default function Strategies() {
  const [universe, setUniverse] = useState<"spy" | "gtaa">("gtaa");
  const view = useApi<StrategyView>(`/api/strategies/ma-timing?universe=${universe}`);
  const theme = useThemeColors();
  const colors = [theme.series[0] ?? "#2a78d6", theme.reference];
  const v = view.data;
  return (
    <div className="grid">
      <Card title="Strategy 1 · 10-month moving-average timing" hint="Faber 2007 · trades the session after each month-end signal · 5 bps costs">
        <div className="toolbar">
          <button aria-pressed={universe === "gtaa"} onClick={() => setUniverse("gtaa")}>
            GTAA (SPY, EFA, IEF, VNQ, DBC)
          </button>
          <button aria-pressed={universe === "spy"} onClick={() => setUniverse("spy")}>
            SPY only
          </button>
        </div>
        <ErrorBanner error={view.error} />
        {view.loading ? (
          <Loading />
        ) : v?.available && v.growth && v.stats ? (
          <>
            <GrowthChart data={v.growth} series={Object.keys(v.stats)} colors={colors} />
            <StatsTable stats={v.stats} colors={colors} />
          </>
        ) : (
          <Empty>
            Not enough daily bars yet for {universe.toUpperCase()}: needs about a year of history (<code>uv run tp-data bars backfill</code>).
          </Empty>
        )}
      </Card>
      {v?.available && (
        <div className="grid cols-2">
          <Card title="Current signals" hint={`invested: ${pct(v.exposure_now, 0)}`}>
            <table>
              <tbody>
                {Object.entries(v.signals ?? {}).map(([sym, on]) => (
                  <tr key={sym}>
                    <td>
                      <strong>{sym}</strong>
                    </td>
                    <td>
                      <span className="pill">
                        <span className={`dot ${on ? "good" : "critical"}`} aria-hidden="true" />
                        {on ? "Above 10-month average: invested" : "Below average: in cash"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <Card title="Recent signal changes" hint="entries and exits; monthly rebalances omitted">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Asset</th>
                  <th className="num">Weight</th>
                </tr>
              </thead>
              <tbody>
                {!v.trades?.length && (
                  <tr>
                    <td colSpan={3} className="muted">No entries or exits in this window.</td>
                  </tr>
                )}
                {(v.trades ?? []).slice().reverse().map((t) => (
                  <tr key={`${t.date}-${t.asset}`}>
                    <td>{t.date}</td>
                    <td>{t.asset}</td>
                    <td className="num">
                      {pct(t.weight_before, 0)} → {pct(t.weight_after, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      )}
    </div>
  );
}
