import { useApi } from "../api";
import { SmileChart } from "../components/charts";
import { Card, Empty, ErrorBanner, Loading } from "../components/ui";
import { money, pct } from "../format";
import type { OptionSummary } from "../types";

/** How one underlying's options are priced in the latest chain snapshot: implied volatility and
 * the straddle's expected move per expiry, and the smile. */
export function OptionPricing({ underlying }: { underlying: string }) {
  const view = useApi<OptionSummary>(`/api/options/${encodeURIComponent(underlying)}`);
  const v = view.data;
  if (view.loading) return <Loading />;
  if (!v?.term.length) {
    return (
      <Card title="Option pricing">
        <ErrorBanner error={view.error} />
        <Empty>No option chain stored for {underlying} yet.</Empty>
      </Card>
    );
  }
  return (
    <div className="grid cols-2 top">
      <Card title="Term structure" hint={`snapshot ${new Date(v.snapshot_at ?? "").toLocaleString()} · spot ${money(v.spot ?? null, true)}`}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Expiry</th>
                <th className="num">Days</th>
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
  );
}
