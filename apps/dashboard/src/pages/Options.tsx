import { useApi } from "../api";
import { SmileChart } from "../components/charts";
import { Card, Empty, ErrorBanner, Loading } from "../components/ui";
import { money, num, pct } from "../format";
import type { OptionSummary, SmileFit } from "../types";

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
                <th className="num" title="25-delta call IV minus put IV on the SVI fit (negative: puts dearer)">
                  Skew
                </th>
                <th title="SVI fit to the out-of-the-money quotes: error in volatility points, and any arbitrage in the smile">Smile fit</th>
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
                  <td className="num">{t.fit?.skew_25d != null ? `${num(t.fit.skew_25d * 100, 1)} pts` : "—"}</td>
                  <td className="small">{fitLabel(t.fit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Volatility smile" hint={`${v.smile_expiration ?? "nearest expiry 20+ days out"}${v.smile_fit?.length ? " · line: SVI fit" : ""}`}>
        <SmileChart data={v.smile} spot={v.spot} fit={v.smile_fit} />
        <p className="muted small">{skewLine(v)}</p>
      </Card>
    </div>
  );
}

export function fitLabel(fit: SmileFit | null | undefined): string {
  if (!fit) return "too few quotes";
  const flags = [fit.butterfly_arbitrage && "butterfly arbitrage", fit.calendar_arbitrage && "below the earlier expiry"].filter(Boolean);
  return `±${num(fit.rmse_iv * 100, 1)} pts${flags.length ? ` · ${flags.join(", ")}` : ""}`;
}

/** The smile in words: how much dearer the downside is, the term structure's slope, and any
 * quotes that can't all be right. */
export function skewLine(v: OptionSummary): string {
  const s = v.skew;
  const parts: string[] = [];
  if (s?.skew_25d != null) {
    const pts = Math.abs(s.skew_25d * 100).toFixed(1);
    parts.push(s.skew_25d < 0 ? `25-delta puts trade ${pts} volatility points over calls` : `25-delta calls trade ${pts} points over puts (unusual for a stock)`);
  }
  if (s?.shape && s.ratio != null) {
    const what = { contango: "longer-dated options dearer, the calm norm", flat: "flat", backwardation: "near-term options dearer: the market expects a rough few weeks" }[s.shape];
    parts.push(`90-day IV ${num(s.ratio, 2)}x the 30-day (${what})`);
  }
  const q = v.quote_arbitrage;
  if (q && (q.vertical || q.butterfly)) parts.push(`${q.vertical + q.butterfly} call quotes inconsistent with each other (stale or bad quotes)`);
  return parts.length ? `${parts.join("; ")}.` : "";
}
