import { useState } from "react";
import { useApi } from "../api";
import { FidelityFreshness } from "../components/broker";
import { Card, Delta, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { money, pct, qty } from "../format";
import type { Trade, Trades as TradesData } from "../types";

const SOURCES = [
  { key: "all", label: "All" },
  { key: "mine", label: "Mine" },
  { key: "paper", label: "Paper bot" },
] as const;
type Source = (typeof SOURCES)[number]["key"];

/** Every trade in one place: yours (from the brokerage sync) and the paper bot's fills, each
 * with the profit or loss it locked in. */
export default function Trades() {
  const [source, setSource] = useState<Source>("all");
  const trades = useApi<TradesData>(`/api/trades?source=${source}&limit=500`, 60_000);
  const t = trades.data;
  if (trades.loading && !t) return <Loading />;
  const mine = t?.mine;
  const paper = t?.paper;
  return (
    <div className="grid">
      <ErrorBanner error={trades.error} />
      {source !== "paper" && <FidelityFreshness freshness={t?.freshness} onSynced={trades.reload} />}
      <div className="grid tiles">
        {source !== "paper" && (
          <>
            <Stat
              label="Your realized P&L"
              value={mine ? <Delta value={mine.realized} kind="money" /> : "—"}
              sub={mine ? <><Delta value={mine.realized_ytd} kind="money" /> this year{mine.since ? ` · history from ${mine.since}` : ""}</> : "No brokerage history synced"}
            />
            <Stat
              label="Your closed trades"
              value={mine ? mine.closed_trades : "—"}
              sub={mine?.win_rate != null ? `${pct(mine.win_rate, 0)} closed at a profit` : null}
            />
          </>
        )}
        {source !== "mine" && (
          <>
            <Stat
              label="Paper bot P&L"
              value={paper?.pnl != null ? <Delta value={paper.pnl} kind="money" /> : "—"}
              sub={paper ? <>open positions included · <a href="#/paper">by strategy</a></> : "No paper trades yet"}
            />
            <Stat
              label="Paper fills"
              value={paper ? paper.fills : "—"}
              sub={paper ? <><Delta value={paper.realized} kind="money" /> realized on {paper.closed_trades} closing fills</> : null}
            />
          </>
        )}
      </div>

      <Card title="Trades" hint="newest first; realized P&L by average cost">
        <div className="toolbar">
          {SOURCES.map((s) => (
            <button key={s.key} aria-pressed={source === s.key} onClick={() => setSource(s.key)}>
              {s.label}
            </button>
          ))}
          {mine && mine.unknown_cost > 0 && source !== "paper" && (
            <span className="muted small">
              {mine.unknown_cost} sale{mine.unknown_cost === 1 ? "" : "s"} of positions bought before the synced history: cost unknown, left out of realized P&L.
            </span>
          )}
        </div>
        {t && t.trades.length > 0 ? (
          <TradeTable rows={t.trades} />
        ) : (
          <Empty>
            No trades yet. Yours appear after <code>uv run tp-broker sync</code>; the paper bot's after its first fills (see the Paper tab).
          </Empty>
        )}
      </Card>
    </div>
  );
}

export function TradeTable({ rows, compact = false }: { rows: Trade[]; compact?: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Account</th>
            <th>Trade</th>
            {!compact && <th className="num">Qty</th>}
            {!compact && <th className="num">Price</th>}
            {!compact && <th className="num">Amount</th>}
            <th className="num">Realized</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.date}-${r.source}-${r.symbol}-${i}`}>
              <td>{r.date}</td>
              <td className={r.source === "paper" ? "" : "muted"}>
                {r.source === "paper" ? <span className="source-tag">Paper</span> : null} {r.source === "paper" ? r.strategy : r.account}
              </td>
              <td title={r.pending ? "From today's change in your positions: the transaction itself reaches your history tomorrow" : r.symbol}>
                {r.pending && <span className="source-tag">pending</span>} {r.action} {compact ? `${qty(r.quantity)} ` : ""}
                <strong>{r.label}</strong>
                {compact && r.price != null ? <span className="muted"> @ {money(r.price, true)}</span> : null}
              </td>
              {!compact && <td className="num">{qty(r.quantity)}</td>}
              {!compact && <td className="num">{money(r.price, true)}</td>}
              {!compact && <td className="num">{money(r.amount, true)}</td>}
              <td className="num">
                {r.realized_pnl != null ? (
                  <Delta value={r.realized_pnl} kind="money" />
                ) : !r.cost_known ? (
                  <span className="muted small" title="Bought before the synced history starts">
                    cost unknown
                  </span>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
