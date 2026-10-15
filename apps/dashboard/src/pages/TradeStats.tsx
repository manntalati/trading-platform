import { useState } from "react";
import { useApi } from "../api";
import { BarList, Card, Delta, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { money, pct, signedMoney } from "../format";
import type { TradeGroup, TradeKind, TradeStats, TripView } from "../types";

const KINDS: { key: TradeKind; label: string }[] = [
  { key: "calls", label: "Calls" },
  { key: "puts", label: "Puts" },
  { key: "shares", label: "Shares" },
  { key: "options_sold", label: "Options sold" },
];

function days(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v < 1) return "same day";
  return `${Math.round(v)} day${Math.round(v) === 1 ? "" : "s"}`;
}

function signed(v: number | null | undefined, digits = 0): string {
  return pct(v, digits, true);
}

/** How you trade, from your own closed round trips (buys matched to sales first in, first out). */
export default function TradeStatsView() {
  const res = useApi<TradeStats>("/api/trades/stats", 300_000);
  const [kind, setKind] = useState<TradeKind>("all");
  const s = res.data;
  if (res.loading && !s) return <Loading />;
  if (!s?.synced) {
    return (
      <>
        <ErrorBanner error={res.error} />
        <Empty>
          No brokerage history yet. Link Fidelity (<code>uv run tp-broker link</code>) and sync (<code>uv run tp-broker sync</code>).
        </Empty>
      </>
    );
  }
  const by = s.by_kind!;
  const all = by.all;
  if (!all.trades) return <Empty>No closed trades in the synced history yet: they appear once you sell something you bought.</Empty>;
  const buckets = s.holding?.[kind] ?? [];
  const holding = Object.fromEntries(buckets.map((b) => [b.bucket, b.trades]));
  const held = buckets.reduce((n, b) => n + b.trades, 0);
  const share = (v: number) => (v ? `${v} · ${pct(v / Math.max(held, 1), 0)}` : "—");
  return (
    <div className="grid">
      <ErrorBanner error={res.error} />
      <div className="grid tiles">
        <Stat label="Closed trades" value={<span className="mono">{all.trades}</span>} sub={s.since ? `since ${s.since}` : null} />
        <Stat
          label="Win rate"
          value={<span className="mono">{pct(all.win_rate, 0)}</span>}
          sub={all.profit_factor != null ? `profit factor ${all.profit_factor.toFixed(2)}` : null}
        />
        <Stat
          label="Typical return"
          value={<span className="mono">{signed(all.median_return)}</span>}
          sub={<>median per trade · {signed(all.weighted_return, 1)} on all the money put in</>}
        />
        <Stat label="Realized" value={<Delta value={all.total_pnl} kind="money" />} sub={`winners ${signed(all.avg_win)}, losers ${signed(all.avg_loss)} on average`} />
      </div>

      {(s.observations ?? []).length > 0 && (
        <Card title="What the numbers say">
          <ul className="observations">
            {s.observations!.map((o) => (
              <li key={o}>{o}</li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="By kind" hint="long positions, except options sold to open">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th />
                <th className="num">Trades</th>
                <th className="num">Win rate</th>
                <th className="num">Typical</th>
                <th className="num">Avg winner</th>
                <th className="num">Avg loser</th>
                <th className="num">Held (median)</th>
                <th className="num">Winners held</th>
                <th className="num">Losers held</th>
                <th className="num">Expired</th>
                <th className="num">Realized</th>
              </tr>
            </thead>
            <tbody>
              {KINDS.map(({ key, label }) => (
                <KindRow key={key} label={label} g={by[key]} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid cols-2 top">
        <Card
          title="How long you hold"
          hint={
            <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value as TradeKind)}>
              <option value="all">All</option>
              {KINDS.filter((k) => by[k.key]?.trades).map((k) => (
                <option key={k.key} value={k.key}>
                  {k.label}
                </option>
              ))}
            </select>
          }
        >
          <BarList data={holding} keepOrder format={share} />
          <p className="small muted">
            Median {days(by[kind]?.median_days)}; winners {days(by[kind]?.median_days_winners)}, losers {days(by[kind]?.median_days_losers)}.
            {s.open?.positions ? ` ${s.open.positions} positions still open, typically held ${days(s.open.median_age_days)} so far.` : ""}
          </p>
        </Card>
        <Card title="Realized by month">
          <MonthBars months={s.monthly ?? []} />
        </Card>
      </div>

      <div className="grid cols-2 top">
        <Card title="Best trades">
          <TripList trips={s.best ?? []} />
        </Card>
        <Card title="Worst trades">
          <TripList trips={s.worst ?? []} />
        </Card>
      </div>
      {(s.unknown_cost ?? 0) > 0 && (
        <p className="small muted">
          {s.unknown_cost} sale{s.unknown_cost === 1 ? "" : "s"} of positions bought before the synced history: no known cost, left out.
        </p>
      )}
    </div>
  );
}

function KindRow(props: { label: string; g: TradeGroup | undefined }) {
  const g = props.g;
  if (!g || !g.trades) return null;
  return (
    <tr>
      <td>{props.label}</td>
      <td className="num mono">{g.trades}</td>
      <td className="num mono">{pct(g.win_rate, 0)}</td>
      <td className="num mono">{signed(g.median_return)}</td>
      <td className="num mono">{signed(g.avg_win)}</td>
      <td className="num mono">{signed(g.avg_loss)}</td>
      <td className="num mono">{days(g.median_days)}</td>
      <td className="num mono">{days(g.median_days_winners)}</td>
      <td className="num mono">{days(g.median_days_losers)}</td>
      <td className="num mono">{g.expired || "—"}</td>
      <td className="num">
        <Delta value={g.total_pnl} kind="money" />
      </td>
    </tr>
  );
}

function TripList(props: { trips: TripView[] }) {
  if (!props.trips.length) return <Empty>None yet.</Empty>;
  return (
    <ul className="trip-list">
      {props.trips.map((t) => (
        <li key={`${t.label}${t.opened}${t.closed}${t.pnl}`}>
          <span className="trip-label">{t.label}</span>
          <span className="muted small">
            {t.side === "short" ? "sold " : ""}
            {t.opened} → {t.closed} · {days(t.days)} · {t.exit}
          </span>
          <span className={`mono ${t.pnl >= 0 ? "up" : "down"}`}>
            {signed(t.return_pct)} · {signedMoney(t.pnl)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function MonthBars(props: { months: { month: string; pnl: number; trades: number }[] }) {
  const m = props.months;
  if (!m.length) return <Empty>No closed trades yet.</Empty>;
  const peak = Math.max(...m.map((x) => Math.abs(x.pnl)), 1);
  return (
    <div className="month-bars" role="img" aria-label="Realized profit and loss by month">
      {m.map((x) => (
        <div key={x.month} className="month" title={`${x.month}: ${signedMoney(x.pnl)} on ${x.trades} trades`}>
          <div className="month-track">
            <span className={x.pnl >= 0 ? "pos" : "neg"} style={{ height: `${(Math.abs(x.pnl) / peak) * 50}%` }} />
          </div>
          <span className="month-label">{x.month.slice(2)}</span>
        </div>
      ))}
      <span className="sr-only">{m.map((x) => `${x.month} ${money(x.pnl)}`).join(", ")}</span>
    </div>
  );
}
