import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, getJson, postJson, useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, go, Loading, Stat, SubNav } from "../components/ui";
import { money, num, pct, qty, signedMoney } from "../format";
import type { LiveState } from "../live";
import {
  isSpread,
  type ChatTurn,
  type FetchState,
  type Pick,
  type Research as ResearchData,
  type ResearchIdea,
  type SpreadPick,
  type SymbolResearch,
  type VolContext,
} from "../types";
import Ideas from "./Ideas";
import { PriceHistory } from "./Market";
import { OptionPricing } from "./Options";
import { AskPanel, Scenarios } from "./Positions";

const VIEWS = [
  { key: "ideas", label: "Options ideas" },
  { key: "lookup", label: "Look up a symbol" },
  { key: "checks", label: "Portfolio checks" },
];

/** What to look at next: ranked swing, LEAPS and bearish setups with the contract each would
 * use; any symbol in depth (and fetched if it's new); and rules-based checks on the portfolio. */
export default function Research({ live, sub, detail }: { live: LiveState; sub: string | null; detail: string | null }) {
  const current = VIEWS.some((v) => v.key === sub) ? sub! : "ideas";
  return (
    <div className="grid">
      <SubNav base="research" current={current} items={VIEWS} />
      {current === "ideas" && <OptionsIdeas />}
      {current === "lookup" && <Lookup symbol={detail?.toUpperCase() ?? null} live={live} />}
      {current === "checks" && <Ideas />}
    </div>
  );
}

// -- ranked ideas -------------------------------------------------------------------------------

const KINDS = [
  {
    key: "swing",
    label: "Swing calls",
    rule: "Uptrends, ranked by 6-month momentum, strength against the S&P 500 and a calmer entry (a pullback). Contract: 30 to 100 days out, delta about 0.55; a debit spread instead when options are expensive.",
  },
  {
    key: "leaps",
    label: "LEAPS",
    rule: "Long, steady uptrends with shallow drawdowns. Contract: 9+ months out, delta about 0.80, the one paying the least time value per year: a stand-in for 100 shares.",
  },
  {
    key: "bearish",
    label: "Bearish puts",
    rule: "Downtrends lagging the S&P 500. Contract: 30 to 100 days out, delta about −0.55; a debit spread when options are expensive.",
  },
] as const;
type Kind = (typeof KINDS)[number]["key"];

function OptionsIdeas() {
  const data = useApi<ResearchData>("/api/research", 300_000);
  const [kind, setKind] = useState<Kind>("swing");
  const r = data.data;
  if (data.loading && !r) return <Loading />;
  if (!r) return <ErrorBanner error={data.error} />;
  const info = KINDS.find((k) => k.key === kind)!;
  const ideas = r[kind];
  return (
    <div className="grid">
      <ErrorBanner error={data.error} />
      <div className="banner">
        {r.scanned} names scanned, {r.with_chains} with option chains
        {r.chains_as_of ? ` (as of ${new Date(r.chains_as_of).toLocaleString()})` : ""}. IV rank needs 20 days of daily snapshots
        {r.iv_history_days < 20 ? `: ${r.iv_history_days} so far, so implied vs realized volatility stands in.` : "."}{" "}
        Add your own tickers in <code>config/universes.local.toml</code>, or <a href="#/research/lookup">look one up</a> to fetch it now.
      </div>
      <div className="toolbar">
        {KINDS.map((k) => (
          <button key={k.key} aria-pressed={kind === k.key} onClick={() => setKind(k.key)}>
            {k.label} ({r[k.key].length})
          </button>
        ))}
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        {info.rule}
      </p>
      {ideas.length ? (
        <div className="grid cols-2 top">
          {ideas.map((i) => (
            <IdeaCard key={`${i.kind}-${i.symbol}`} idea={i} />
          ))}
        </div>
      ) : (
        <Card>
          <Empty>No {info.label.toLowerCase()} setups right now.</Empty>
        </Card>
      )}
      {r.without_chains.length > 0 && (
        <p className="muted small">
          Ranked without option chains (no contract picked): {r.without_chains.join(", ")}. They get chains once they're in your research list.
        </p>
      )}
      <p className="muted small">{r.disclaimer}</p>
    </div>
  );
}

export function IdeaCard({ idea }: { idea: ResearchIdea }) {
  return (
    <article className={`card idea ${idea.kind === "bearish" ? "attention" : "info"}`}>
      <div className="idea-head">
        <a href={`#/research/lookup/${encodeURIComponent(idea.symbol)}`} className="idea-symbol">
          {idea.symbol}
        </a>
        <span className="muted">{money(idea.spot, true)}</span>
        <span className="spacer" />
        <span className="small muted" title="rank among the names scanned (1 = best)">
          score {pct(idea.score, 0)}
        </span>
      </div>
      <div className="tags">
        {idea.setup.tags.map((t) => (
          <span key={t} className="pill">
            {t}
          </span>
        ))}
        <VolTag vol={idea.vol} />
      </div>
      {idea.pick ? <PickSummary pick={idea.pick} /> : <p className="muted small">No contract picked.</p>}
      <ul className="why">
        {idea.why.map((w) => (
          <li key={w}>{w}</li>
        ))}
      </ul>
      {idea.risks.length > 0 && (
        <ul className="risks">
          {idea.risks.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {idea.alternative && (
        <p className="small muted">
          Alternative: <PickLine pick={idea.alternative} />
        </p>
      )}
      <a className="small" href={`#/research/lookup/${encodeURIComponent(idea.symbol)}`}>
        {idea.symbol} in depth, scenarios and Ask Claude →
      </a>
    </article>
  );
}

function VolTag({ vol }: { vol: VolContext }) {
  if (vol.iv30 == null) return null;
  const label = vol.rank != null ? `IV rank ${pct(vol.rank, 0)}` : vol.iv_hv != null ? `IV ${num(vol.iv_hv, 1)}x realized` : `IV ${pct(vol.iv30, 0)}`;
  return <span className={`pill ${vol.rich ? "rich" : ""}`}>{vol.rich ? `options expensive · ${label}` : label}</span>;
}

function strike(k: number): string {
  return k.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** One line: what you'd buy (or the spread) and what it costs. */
export function PickLine({ pick }: { pick: Pick }) {
  if (isSpread(pick)) {
    return (
      <>
        {pick.long.right} spread ${strike(pick.long.strike)}/${strike(pick.short.strike)} {pick.long.expiration} for{" "}
        {money(pick.debit, true)} ({money(pick.cost)})
      </>
    );
  }
  return (
    <>
      {pick.label} at {money(pick.price, true)} ({money(pick.cost)})
    </>
  );
}

export function PickSummary({ pick }: { pick: Pick }) {
  if (isSpread(pick)) return <SpreadSummary s={pick} />;
  const leaps = pick.capital_freed != null;
  return (
    <div className="pick">
      <div className="pick-title">
        Buy <strong>{pick.label}</strong> at {money(pick.price, true)} <span className="muted">({money(pick.cost)} a contract)</span>
      </div>
      <div className="pick-facts">
        <Fact label="Breakeven" value={money(pick.breakeven, true)} sub={pct(pick.breakeven_move_pct, 1, true)} />
        <Fact label="Odds of profit" value={pct(pick.prob_profit, 0)} sub="at expiry" />
        <Fact label="Delta" value={num(pick.delta, 2)} sub={leaps ? `acts like ${num(pick.shares_equivalent, 0)} shares` : `${pick.dte} days`} />
        {leaps ? (
          <>
            <Fact label="Frees" value={money(pick.capital_freed)} sub={`vs ${money(pick.stock_cost)} for 100 shares`} />
            <Fact label="Time value" value={`${pct(pick.carry_per_year_pct, 1)}/yr`} sub={pick.breakeven_cagr != null ? `breakeven ${pct(pick.breakeven_cagr, 1, true)}/yr` : null} />
          </>
        ) : (
          <>
            <Fact label="Decay" value={`${signedMoney(pick.theta_day)}/day`} sub={`IV ${pct(pick.iv, 0)}`} />
            <Fact label="Spread" value={pct(pick.spread_pct, 0)} sub={pick.open_interest != null ? `OI ${qty(pick.open_interest)}` : null} />
          </>
        )}
      </div>
    </div>
  );
}

function SpreadSummary({ s }: { s: SpreadPick }) {
  return (
    <div className="pick">
      <div className="pick-title">
        Buy <strong>{s.long.label}</strong>, sell the ${strike(s.short.strike)} for a {money(s.debit, true)} debit{" "}
        <span className="muted">({money(s.cost)} a spread)</span>
      </div>
      <div className="pick-facts">
        <Fact label="Max gain" value={money(s.max_gain)} sub={`${num(s.reward_risk, 1)}x the cost`} />
        <Fact label="Breakeven" value={money(s.breakeven, true)} sub={pct(s.breakeven_move_pct, 1, true)} />
        <Fact label="Odds of profit" value={pct(s.prob_profit, 0)} sub="at expiry" />
        <Fact label="Decay" value={`${signedMoney(s.theta_day)}/day`} sub={`${s.long.dte} days`} />
      </div>
    </div>
  );
}

function Fact({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="fact">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  );
}

// -- one symbol in depth ------------------------------------------------------------------------

function Lookup({ symbol, live }: { symbol: string | null; live: LiveState }) {
  const [draft, setDraft] = useState(symbol ?? "");
  useEffect(() => setDraft(symbol ?? ""), [symbol]);
  return (
    <div className="grid">
      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          const s = draft.trim().toUpperCase();
          if (s) go(`research/lookup/${encodeURIComponent(s)}`);
        }}
      >
        <input aria-label="Symbol" placeholder="Ticker, e.g. AAPL" value={draft} onChange={(e) => setDraft(e.target.value)} style={{ width: 160 }} />
        <button type="submit" className="primary">
          Look up
        </button>
        <span className="muted small">Trend, option pricing, the contracts each approach would use, and Claude to pressure-test it.</span>
      </form>
      {symbol ? <SymbolDepth key={symbol} symbol={symbol} live={live} /> : <Empty>Enter a ticker to see it in depth.</Empty>}
    </div>
  );
}

function SymbolDepth({ symbol, live }: { symbol: string; live: LiveState }) {
  const [data, setData] = useState<SymbolResearch | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [thread, setThread] = useState<ChatTurn[]>([]);

  useEffect(() => {
    let cancelled = false;
    getJson<SymbolResearch>(`/api/research/symbol/${encodeURIComponent(symbol)}`)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setMissing(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) setMissing(true);
        else setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, tick]);

  if (missing) return <FetchTicker symbol={symbol} onDone={() => setTick((t) => t + 1)} />;
  if (error) return <ErrorBanner error={error} />;
  if (!data) return <Loading />;
  const s = data.stock;
  const r = s.returns;
  const v = data.vol;
  const p = data.picks;
  return (
    <div className="grid">
      <div className="grid tiles">
        <Stat label={`${symbol} price`} value={money(data.spot, true)} sub={<><Delta value={s.day_change_pct} /> today{data.held ? ` · you hold ${data.held}` : ""}</>} />
        <Stat label="Trend" value={data.setup.direction} sub={data.setup.tags.join(" · ") || "no clear setup"} />
        <Stat label="3-month return" value={<Delta value={r?.["3M"]} digits={1} />} sub={<>1 month <Delta value={r?.["1M"]} digits={1} /> · 1 year <Delta value={r?.["1Y"]} digits={1} /></>} />
        <Stat
          label="Options pricing"
          value={v.iv30 != null ? `IV ${pct(v.iv30, 0)}` : "—"}
          sub={
            v.iv30 == null
              ? "no chain yet"
              : `${v.rank != null ? `rank ${pct(v.rank, 0)} · ` : ""}${v.iv_hv != null ? `${num(v.iv_hv, 1)}x realized ${pct(v.hv20, 0)}` : ""}${v.rich ? " · expensive" : ""}`
          }
        />
      </div>
      {v.event && (
        <div className="banner">
          The option market is pricing an event (often earnings) before {v.event.by}: at-the-money IV is {pct(v.event.iv, 0)} for that expiry vs{" "}
          {pct(v.event.iv_after, 0)} after it. Options held through it usually lose value when it passes.
        </div>
      )}
      {data.chain_age_days != null && data.chain_age_days > 3 && (
        <div className="banner">The option chain is {data.chain_age_days} days old: refresh it below or wait for the daily snapshot.</div>
      )}
      {Object.keys(p).length ? (
        <div className="grid cols-2 top">
          {p.swing_call && <PickCard title="Swing call" hint="30-100 days, delta ~0.55" pick={p.swing_call} />}
          {p.call_spread && <PickCard title="Call debit spread" hint="cheaper; capped gain; less hurt by an IV drop" pick={p.call_spread} />}
          {p.leaps_call && <PickCard title="LEAPS call" hint="stand-in for 100 shares" pick={p.leaps_call} />}
          {p.swing_put && <PickCard title="Swing put" hint="if you're bearish" pick={p.swing_put} />}
          {p.put_spread && <PickCard title="Put debit spread" pick={p.put_spread} />}
        </div>
      ) : (
        <FetchTicker symbol={symbol} onDone={() => setTick((t) => t + 1)} reason="No option chain stored for it yet." />
      )}
      {p.swing_call?.scenarios && <Scenarios grid={p.swing_call.scenarios} cost={p.swing_call.price} quantity={1} />}
      <AskPanel
        endpoint="/api/research/ask"
        symbol={symbol}
        title={`Ask Claude about ${symbol}`}
        ask={data.ask}
        thread={thread}
        onThread={setThread}
        suggestions={[
          `Is ${symbol} a good swing trade here, and which structure?`,
          `Would a LEAPS on ${symbol} make sense instead of shares?`,
          `Any earnings or news before these expiries?`,
        ]}
      />
      <PriceHistory symbol={symbol} live={live} />
      <OptionPricing underlying={symbol} />
      <p className="muted small">{data.disclaimer}</p>
    </div>
  );
}

function PickCard({ title, hint, pick }: { title: string; hint?: string; pick: Pick }) {
  return (
    <Card title={title} hint={hint}>
      <PickSummary pick={pick} />
    </Card>
  );
}

/** Pull two years of bars and today's option chain for a ticker the platform doesn't have yet. */
export function FetchTicker({ symbol, onDone, reason }: { symbol: string; onDone: () => void; reason?: string }) {
  const [state, setState] = useState<FetchState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const done = useRef(onDone);
  done.current = onDone;
  const running = state?.running ?? false;

  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(async () => {
      try {
        const next = await getJson<FetchState>("/api/research/fetch");
        setState(next);
        if (!next.running && next.ok) done.current();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, 2000);
    return () => window.clearInterval(id);
  }, [running]);

  async function start() {
    setError(null);
    try {
      setState(await postJson<FetchState>("/api/research/fetch", { symbol }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Card title={`Fetch ${symbol}`}>
      <p className="muted">
        {reason ?? `The platform has no price history for ${symbol} yet.`} Fetching pulls two years of daily bars and today's option chain from Alpaca
        (a minute or so). To keep it updated daily, add it to <code>[research] symbols</code> in <code>config/universes.local.toml</code>.
      </p>
      <div className="toolbar">
        <button className="primary" onClick={start} disabled={running}>
          {running ? "Fetching…" : `Fetch ${symbol}`}
        </button>
        {state?.finished_at && !running && (
          <span className="small muted">
            <span className={`dot ${state.ok ? "good" : "critical"}`} aria-hidden="true" /> {state.message}
          </span>
        )}
      </div>
      {error && <ErrorBanner error={error} />}
    </Card>
  );
}
