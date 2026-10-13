import { useEffect, useRef, useState } from "react";
import { useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, Stat } from "../components/ui";
import { money, nyTime, signedMoney, timeAgo } from "../format";
import type { LiveState } from "../live";
import {
  isOption,
  type MarketRegime,
  type PaperStatus,
  type Portfolio,
  type Positions,
  type Quote,
  type Research,
  type Status,
  type Trades,
  type Validation,
} from "../types";
import { PickLine } from "./Research";
import { TradeTable } from "./Trades";

export interface AttentionItem {
  level: "risk" | "warn" | "info";
  title: string;
  text: string;
  href: string;
}

/** Everything that wants a decision or a fix, most urgent first: options about to be exercised
 * or expire, actions the rules flag on positions, paper orders waiting, data gone stale. */
export function attentionItems(
  positions: Positions | null,
  status: Status | null,
  pendingApprovals: number,
  validation: Validation | null = null,
): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const p of positions?.positions ?? []) {
    const href = `#/portfolio/positions/${encodeURIComponent(p.symbol)}`;
    for (const n of p.notes) {
      if (n.level === "risk") items.push({ level: "risk", title: `${p.label}: ${n.title}`, text: n.text, href });
    }
    for (const a of p.actions ?? []) {
      if (a.level !== "info") items.push({ level: a.level === "attention" ? "risk" : "warn", title: `${p.label}: ${a.title}`, text: a.text, href });
    }
    if (isOption(p) && p.stats.days_left >= 1 && p.stats.days_left <= 7 && !items.some((i) => i.href === href)) {
      items.push({ level: "warn", title: `${p.label} expires in ${p.stats.days_left.toFixed(0)} days`, text: p.notes[0]?.text ?? "", href });
    }
  }
  if (pendingApprovals > 0) {
    items.push({
      level: "warn",
      title: `${pendingApprovals} paper order${pendingApprovals === 1 ? "" : "s"} waiting for your approval`,
      text: "Approve or reject them before the next open.",
      href: "#/paper",
    });
  }
  const unproven = (validation?.strategies ?? []).filter((v) => v.on_paper && !v.report?.gate.passed);
  if (unproven.length) {
    items.push({
      level: "info",
      title: `${unproven.length} paper strateg${unproven.length === 1 ? "y hasn't" : "ies haven't"} passed the backtest gate`,
      text: `${unproven.map((v) => v.name).join(", ")}: validate them to see what the plan's stage-1 checks say.`,
      href: "#/paper/strategies",
    });
  }
  if (status?.bars.stale) {
    items.push({
      level: "info",
      title: "Daily prices are behind",
      text: `Latest ${status.bars.latest_session}, expected ${status.bars.expected_session}: check the bars job.`,
      href: "#/system",
    });
  }
  if (status?.dashboard?.stale) {
    items.push({ level: "info", title: "The dashboard is older than its code", text: "Restart tp-api to rebuild it.", href: "#/system" });
  }
  const order = { risk: 0, warn: 1, info: 2 };
  return items.sort((a, b) => order[a.level] - order[b.level]);
}

export default function Home({ live, pendingApprovals }: { live: LiveState; pendingApprovals: number }) {
  const status = useApi<Status>("/api/status", 60_000);
  const portfolio = useApi<Portfolio>("/api/portfolio", 120_000);
  const positions = useApi<Positions>("/api/positions", 60_000);
  const research = useApi<Research>("/api/research", 600_000);
  const trades = useApi<Trades>("/api/trades?limit=8", 60_000);
  const regime = useApi<MarketRegime>("/api/market/regime", 600_000);
  const validation = useApi<Validation>("/api/strategies/validation", 600_000);
  const paper = useApi<PaperStatus>("/api/paper", 60_000);
  const s = status.data;
  const lp = live.portfolio;
  const pnl = portfolio.data?.pnl;
  const bot = paper.data?.bot;
  const paperPnl = trades.data?.paper?.pnl;
  const theta = positions.data?.totals?.theta_day;
  const attention = attentionItems(positions.data, s, pendingApprovals, validation.data);
  const now = regime.data?.current;
  const ideas = research.data ? [...research.data.swing.slice(0, 2), ...research.data.leaps.slice(0, 1), ...research.data.bearish.slice(0, 1)] : [];

  return (
    <div className="grid">
      <ErrorBanner error={status.error} />
      <div className="grid tiles">
        <Stat
          label="Account value"
          value={lp ? money(lp.value) : money(portfolio.data?.total_value)}
          sub={lp ? <><Delta value={lp.day_pnl} kind="money" /> today (<Delta value={lp.day_pnl_pct} />)</> : <a href="#/portfolio">Portfolio</a>}
        />
        <Stat
          label="Total P&L"
          value={pnl ? <Delta value={pnl.total} kind="money" /> : "—"}
          sub={
            pnl ? (
              <>
                <Delta value={pnl.unrealized} kind="money" /> open · <Delta value={pnl.realized} kind="money" /> closed
              </>
            ) : portfolio.loading ? (
              "Loading…"
            ) : (
              "No portfolio synced"
            )
          }
        />
        {theta != null && theta !== 0 && <Stat label="Options time decay" value={`${signedMoney(theta)}/day`} sub={<a href="#/portfolio/positions">your options</a>} />}
        <Stat
          label="Paper bot"
          value={paperPnl != null ? <Delta value={paperPnl} kind="money" /> : bot ? (bot.alive ? "Running" : "Stopped") : "—"}
          sub={
            <>
              <span className={`dot ${bot?.alive ? "good" : bot ? "critical" : ""}`} aria-hidden="true" /> {bot ? (bot.alive ? "running" : "not running") : "not started"} ·{" "}
              <a href="#/paper">paper trading</a>
            </>
          }
        />
        <Stat
          label="Market"
          value={now?.trend ? (now.trend === "up" ? "Uptrend" : "Downtrend") : s ? (s.market.is_open ? "Open" : "Closed") : "—"}
          sub={
            <>
              {now ? `${now.volatility ?? "—"} volatility · ${now.breadth === "broad" ? "most stocks rising" : now.breadth === "narrow" ? "few stocks rising" : "mixed breadth"} · ` : ""}
              {s ? (s.market.is_open ? `closes ${nyTime(s.market.next_close)} ET` : `opens ${nyTime(s.market.next_open)} ET`) : null}
            </>
          }
        />
      </div>

      <div className="grid cols-2 top">
        <Card title="Needs your attention" hint={attention.length ? `${attention.length}` : undefined}>
          {attention.length ? (
            <div className="notes">
              {attention.slice(0, 8).map((i) => (
                <a key={i.title} href={i.href} className={`note idea attention-item ${i.level === "risk" ? "attention" : i.level === "warn" ? "consider" : "info"}`}>
                  <strong>{i.title}</strong>
                  <div>{i.text}</div>
                </a>
              ))}
            </div>
          ) : (
            <Empty>{positions.loading ? "Checking…" : "Nothing urgent."}</Empty>
          )}
        </Card>
        <Card title="Ideas to look at" hint={<a href="#/research">all ideas</a>}>
          {ideas.length ? (
            <div className="notes">
              {ideas.map((i) => (
                <a key={`${i.kind}-${i.symbol}`} href={`#/research/lookup/${encodeURIComponent(i.symbol)}`} className={`note idea attention-item ${i.kind === "bearish" ? "attention" : "info"}`}>
                  <strong>
                    {i.symbol} · {i.kind === "swing" ? "swing" : i.kind === "leaps" ? "LEAPS" : "bearish"}
                  </strong>
                  <div>{i.pick ? <PickLine pick={i.pick} /> : i.why[0]}</div>
                </a>
              ))}
            </div>
          ) : (
            <Empty>{research.loading ? "Scanning…" : "No setups right now."}</Empty>
          )}
        </Card>
      </div>

      <Card title="Live prices" hint={live.lastUpdate ? `updated ${timeAgo(live.lastUpdate)}` : live.source ?? undefined}>
        <LiveQuotes live={live} />
      </Card>

      <Card title="Recent trades" hint={<a href="#/trades">all trades</a>}>
        <ErrorBanner error={trades.error} />
        {trades.data && trades.data.trades.length > 0 ? (
          <TradeTable rows={trades.data.trades} compact />
        ) : (
          <Empty>No trades yet: yours appear after a brokerage sync, the paper bot's after its first fills.</Empty>
        )}
      </Card>
    </div>
  );
}

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
    <a className={`quote ${flash}`} href={`#/research/lookup/${encodeURIComponent(quote.symbol)}`}>
      <div className="sym">{quote.symbol}</div>
      <div className="px">{money(quote.price, true)}</div>
      <Delta value={quote.change_pct} />
    </a>
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
