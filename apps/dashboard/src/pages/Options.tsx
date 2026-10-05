import { useState } from "react";
import { useApi } from "../api";
import { SmileChart } from "../components/charts";
import { Card, Empty, ErrorBanner, Loading } from "../components/ui";
import { money, pct } from "../format";
import type { OptionSummary } from "../types";

export default function Options() {
  const [underlying, setUnderlying] = useState("SPY");
  const [draft, setDraft] = useState("SPY");
  const view = useApi<OptionSummary>(`/api/options/${encodeURIComponent(underlying)}`);
  const v = view.data;
  return (
    <div className="grid">
      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          setUnderlying(draft.trim().toUpperCase() || "SPY");
        }}
      >
        <input aria-label="Underlying" value={draft} onChange={(e) => setDraft(e.target.value)} style={{ width: 110 }} />
        <button type="submit">Show</button>
        {v?.snapshot_at && <span className="small muted">Snapshot {new Date(v.snapshot_at).toLocaleString()} · spot {money(v.spot ?? null, true)}</span>}
      </form>
      <ErrorBanner error={view.error} />
      {view.loading ? (
        <Loading />
      ) : !v?.term.length ? (
        <Card>
          <Empty>
            No chain snapshot for {underlying} yet. The daily job (<code>tp-data options snapshot</code>) collects the [options] universe.
          </Empty>
        </Card>
      ) : (
        <div className="grid cols-2">
          <Card title="Term structure" hint="at-the-money IV and the straddle-implied move per expiry">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Expiry</th>
                    <th className="num">DTE</th>
                    <th className="num">ATM IV</th>
                    <th className="num">Expected move</th>
                  </tr>
                </thead>
                <tbody>
                  {v.term.map((t) => (
                    <tr key={t.expiration}>
                      <td>{t.expiration}</td>
                      <td className="num">{t.dte}</td>
                      <td className="num">{pct(t.atm_iv, 1)}</td>
                      <td className="num">
                        ±{money(t.expected_move, true)} <span className="muted">({pct(t.expected_move_pct, 1)})</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <Card title="Volatility smile" hint="nearest expiry 20+ days out">
            <SmileChart data={v.smile} spot={v.spot} />
          </Card>
        </div>
      )}
    </div>
  );
}
