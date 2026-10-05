import { useEffect, useRef, useState } from "react";
import { useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, Stat } from "../components/ui";
import { money, nyTime, timeAgo } from "../format";
import type { LiveState } from "../live";
import type { Ideas, Quote, Status } from "../types";

function QuoteTile({ quote }: { quote: Quote }) {
  const prev = useRef(quote.price);
  const [flash, setFlash] = useState("");
  useEffect(() => {
    if (prev.current != null && quote.price != null && quote.price !== prev.current) {
      setFlash(quote.price > prev.current ? "flash-up" : "flash-down");
      const id = window.setTimeout(() => setFlash(""), 800);
      prev.current = quote.price;
      return () => window.clearTimeout(id);
    }
    prev.current = quote.price;
  }, [quote.price]);
  return (
    <div className={`quote ${flash}`}>
      <div className="sym">{quote.symbol}</div>
      <div className="px">{money(quote.price, true)}</div>
      <Delta value={quote.change_pct} />
    </div>
  );
}

export function LiveQuotes({ live }: { live: LiveState }) {
  const quotes = Object.values(live.quotes).sort((a, b) => a.symbol.localeCompare(b.symbol));
  if (!quotes.length) {
    return <Empty>No live symbols yet. Sync a portfolio or set a [dashboard] watchlist.</Empty>;
  }
  return (
    <div className="quotes">
      {quotes.map((q) => (
        <QuoteTile key={q.symbol} quote={q} />
      ))}
    </div>
  );
}

export default function Overview({ live }: { live: LiveState }) {
  const status = useApi<Status>("/api/status", 30_000);
  const ideas = useApi<Ideas>("/api/ideas", 300_000);
  const s = status.data;
  const attention = ideas.data?.ideas.filter((i) => i.severity === "attention") ?? [];
  const p = live.portfolio;

  return (
    <div className="grid">
      <ErrorBanner error={status.error} />
      <div className="grid tiles">
        <Stat
          label="Portfolio value"
          value={p ? money(p.value) : "—"}
          sub={p ? <><Delta value={p.day_pnl} kind="money" /> today (<Delta value={p.day_pnl_pct} />)</> : "No portfolio synced"}
        />
        <Stat
          label="Market"
          value={s ? (s.market.is_open ? "Open" : "Closed") : "—"}
          sub={s ? (s.market.is_open ? `Closes ${nyTime(s.market.next_close)} ET` : `Opens ${nyTime(s.market.next_open)} ET`) : null}
        />
        <Stat
          label="Daily bars"
          value={s?.bars.latest_session ?? "—"}
          sub={s ? `${s.bars.symbols} symbols${s.bars.stale ? " · behind schedule" : ""}` : null}
        />
        <Stat
          label="Needs attention"
          value={ideas.data ? attention.length : "—"}
          sub={<a href="#/ideas">See all ideas</a>}
        />
      </div>

      <Card title="Live prices" hint={live.lastUpdate ? `updated ${timeAgo(live.lastUpdate)}` : live.source ?? undefined}>
        <LiveQuotes live={live} />
      </Card>

      <div className="grid cols-2">
        <Card title="Needs attention">
          {attention.length ? (
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {attention.slice(0, 6).map((i) => (
                <li key={i.title} style={{ marginBottom: 6 }}>
                  <strong>{i.title}</strong>
                  <div className="muted">{i.summary}</div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>{ideas.loading ? "Loading…" : "Nothing flagged."}</Empty>
          )}
        </Card>
        <Card title="Accounts">
          {s?.broker.length ? (
            <table>
              <tbody>
                {s.broker.map((b) => (
                  <tr key={`${b.source}-${b.account}`}>
                    <td>{b.institution}</td>
                    <td>{b.account}</td>
                    <td className="num muted">synced {timeAgo(b.synced_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>
              No brokerage synced. Run <code>uv run tp-broker sync</code>.
            </Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
