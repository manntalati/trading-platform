import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, getJson, postJson, useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, go, Loading, Stat, SubNav } from "../components/ui";
import { money, num, nyTime, pct, qty, signedMoney } from "../format";
import type { LiveState } from "../live";
import {
  isCalendar,
  isCondor,
  isCreditSpread,
  isSpread,
  type CalendarPick,
  type ChatTurn,
  type ContractPick,
  type CreditSpreadPick,
  type FetchState,
  type Fundamentals,
  type IdeaList,
  type IronCondorPick,
  type Pick,
  type Research as ResearchData,
  type ResearchIdea,
  type SpreadPick,
  type SymbolResearch,
  type TrackRecord,
  type VolContext,
} from "../types";
import Ideas from "./Ideas";
import { PriceHistory } from "./Market";
import { OptionPricing } from "./Options";
import { AskPanel, Scenarios } from "./Positions";

const VIEWS = [
  { key: "ideas", label: "Options ideas" },
  { key: "track-record", label: "Track record" },
  { key: "lookup", label: "Look up a symbol" },
  { key: "checks", label: "Portfolio checks" },
];

/** What to look at next: ranked swing, LEAPS, bearish and premium-selling setups with the
 * contract each would use; any symbol in depth (and fetched if it's new); and rules-based checks on the portfolio. */
export default function Research({ live, sub, detail }: { live: LiveState; sub: string | null; detail: string | null }) {
  const current = VIEWS.some((v) => v.key === sub) ? sub! : "ideas";
  return (
    <div className="grid">
      <SubNav base="research" current={current} items={VIEWS} />
      {current === "ideas" && <OptionsIdeas />}
      {current === "track-record" && <TrackRecordView />}
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
  {
    key: "income",
    label: "Selling premium",
    rule: "Expensive options (high IV rank, or implied well above realized volatility) on stocks not in a downtrend, richest first. Uptrends: a cash-secured put about 0.25 delta, 25 to 50 days out (a put credit spread caps the risk). No trend: an iron condor. The gain is capped at the premium; size for the maximum loss.",
  },
] as const;
type Kind = (typeof KINDS)[number]["key"];

function OptionsIdeas() {
  const data = useApi<ResearchData>("/api/research", 120_000);
  const [kind, setKind] = useState<Kind>("swing");
  const [updating, setUpdating] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const r = data.data;
  if (data.loading && !r) return <Loading />;
  if (!r) return <ErrorBanner error={data.error} />;
  const info = KINDS.find((k) => k.key === kind)!;
  const ideas = r[kind];
  const dropped = r.run?.dropped?.[kind] ?? [];
  async function update() {
    setUpdating(true);
    setUpdateError(null);
    try {
      await postJson<ResearchData>("/api/research/run", {});
      data.reload();
    } catch (e) {
      setUpdateError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setUpdating(false);
    }
  }
  return (
    <div className="grid">
      <ErrorBanner error={data.error ?? updateError} />
      <div className="run-line">
        <span>
          Updated <strong>{r.run ? nyTime(r.run.as_of) : "just now"}</strong>
          {r.run?.bars_session ? <> · daily bars through {r.run.bars_session}</> : null}
          {newCount(r) > 0 ? <> · <span className="pill new">{newCount(r)} new since the previous session</span></> : null}
        </span>
        <span className="spacer" />
        <span className="muted small">The job scheduler refreshes these whenever new data lands.</span>
        <button onClick={update} disabled={updating}>
          {updating ? "Updating…" : "Update now"}
        </button>
      </div>
      <div className="banner">
        {r.scanned} names scanned, {r.with_chains} with option chains
        {r.chains_as_of ? ` (as of ${new Date(r.chains_as_of).toLocaleString()})` : ""}. IV rank needs 20 days of daily snapshots
        {r.iv_history_days < 20 ? `: ${r.iv_history_days} so far, so implied vs realized volatility stands in.` : "."}{" "}
        Add your own tickers in <code>config/universes.local.toml</code>, or <a href="#/research/lookup">look one up</a> to fetch it now.
      </div>
      <div className="toolbar">
        {KINDS.map((k) => (
          <button key={k.key} aria-pressed={kind === k.key} onClick={() => setKind(k.key)}>
            {k.label} ({r[k.key].length}
            {r[k.key].some((i) => i.new) ? `, ${r[k.key].filter((i) => i.new).length} new` : ""})
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
      {dropped.length > 0 && <p className="muted small">No longer listed since the previous session: {dropped.join(", ")}.</p>}
      {Object.keys(r.errors ?? {}).length > 0 && (
        <p className="muted small">
          Skipped because of an error (details in the tp-api log): {Object.keys(r.errors ?? {}).join(", ")}.
        </p>
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

export function newCount(r: ResearchData): number {
  return KINDS.reduce((n, k) => n + r[k.key].filter((i) => i.new).length, 0);
}

// -- how past recommendations did -----------------------------------------------------------------

const LIST_LABEL: Record<IdeaList, string> = { swing: "Swing", leaps: "LEAPS", bearish: "Bearish", income: "Premium" };

function TrackRecordView() {
  const res = useApi<TrackRecord>("/api/research/track-record", 600_000);
  const [list, setList] = useState<IdeaList | "all">("all");
  const t = res.data;
  if (res.loading && !t) return <Loading />;
  if (!t) return <ErrorBanner error={res.error} />;
  if (!t.recommendations.length) {
    return (
      <Card>
        <Empty>No recommendations stored yet. The job scheduler stores every run from now on; their track record builds up here.</Empty>
      </Card>
    );
  }
  const rows = t.recommendations.filter((r) => list === "all" || r.list === list);
  return (
    <div className="grid">
      <ErrorBanner error={res.error} />
      <p className="muted small" style={{ margin: 0 }}>
        Every idea the platform listed, from the first run it appeared in. <strong>Stock</strong> is the move since then in the direction recommended (a fall
        counts as a gain for a bearish idea); <strong>Position</strong> marks the contracts at the middle of the latest bid and ask (entered at the ask for a
        purchase and the bid for a sale), or at their value at expiry. For premium selling it is the share of the premium kept. Rules applied to data, not trades
        anyone made.
      </p>
      <Card title="By list">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th />
                <th className="num">Ideas</th>
                <th className="num" title="positions worth more than they cost (premium: some of it kept)">Profitable</th>
                <th className="num">Median position</th>
                <th className="num" title="the stock moved the recommended way after 5 sessions">Right way, 5d</th>
                <th className="num" title="after 21 sessions">21d</th>
                <th className="num">Median stock, 21d</th>
                <th className="num">Beat S&amp;P 500</th>
              </tr>
            </thead>
            <tbody>
              {t.summary.map((s) => (
                <tr key={s.list}>
                  <td>{s.name}</td>
                  <td className="num mono">{s.recommendations}</td>
                  <td className="num mono">
                    {pct(s.profitable, 0)} <span className="muted">of {s.marked}</span>
                  </td>
                  <td className="num mono">{pct(s.median_result, 0, true)}</td>
                  <td className="num mono">{pct(s.right_way_5d, 0)}</td>
                  <td className="num mono">{pct(s.right_way_21d, 0)}</td>
                  <td className="num mono">{pct(s.median_move_21d, 1, true)}</td>
                  <td className="num mono">{pct(s.beat_spy, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card
        title="Each recommendation"
        hint={
          <select aria-label="List" value={list} onChange={(e) => setList(e.target.value as IdeaList | "all")}>
            <option value="all">All lists</option>
            {t.summary.map((s) => (
              <option key={s.list} value={s.list}>
                {s.name}
              </option>
            ))}
          </select>
        }
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>First listed</th>
                <th>Idea</th>
                <th>Position</th>
                <th className="num">Stock since</th>
                <th className="num">vs S&amp;P 500</th>
                <th className="num">Position</th>
                <th className="num">P&amp;L (1 lot)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.list}${r.symbol}${r.recommended_at}${r.legs.map((l) => l.contract).join()}`}>
                  <td className="mono small">{r.recommended_at.slice(0, 10)}</td>
                  <td>
                    <a href={`#/research/lookup/${encodeURIComponent(r.symbol)}`}>{r.symbol}</a> <span className="muted small">{LIST_LABEL[r.list]}</span>
                    {r.runs > 1 && <span className="muted small"> · listed {r.runs} times</span>}
                  </td>
                  <td className="small">
                    {r.structure}
                    {r.legs.map((l) => (
                      <div key={l.contract} className="muted leg">
                        {l.side === "sell" ? "sold " : "bought "}
                        {l.label}
                      </div>
                    ))}
                  </td>
                  <td className="num mono">
                    <Delta value={r.move} digits={1} />
                  </td>
                  <td className="num mono">
                    <Delta value={r.move_vs_spy} digits={1} />
                  </td>
                  <td className="num mono">
                    {r.result != null ? <Delta value={r.result} digits={0} /> : <span className="muted">—</span>}
                    {r.status === "expired" && <div className="muted small">expired</div>}
                  </td>
                  <td className="num mono">{r.pnl != null ? <span className={r.pnl >= 0 ? "up" : "down"}>{signedMoney(r.pnl)}</span> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

export function IdeaCard({ idea }: { idea: ResearchIdea }) {
  return (
    <article className={`card idea ${idea.kind === "bearish" ? "attention" : idea.kind === "income" ? "consider" : "info"}`}>
      <div className="idea-head">
        <a href={`#/research/lookup/${encodeURIComponent(idea.symbol)}`} className="idea-symbol">
          {idea.symbol}
        </a>
        {idea.new && <span className="pill new">new</span>}
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
  const label =
    vol.rank != null
      ? `IV rank ${pct(vol.rank, 0)}`
      : vol.iv_rv != null
        ? `IV ${num(vol.iv_rv, 1)}x forecast`
        : vol.iv_hv != null
          ? `IV ${num(vol.iv_hv, 1)}x realized`
          : `IV ${pct(vol.iv30, 0)}`;
  return (
    <>
      <span className={`pill ${vol.rich ? "rich" : ""}`}>{vol.rich ? `options expensive · ${label}` : label}</span>
      {vol.term_shape === "backwardation" && <span className="pill">near-term IV dearer</span>}
    </>
  );
}

function strike(k: number): string {
  return k.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** One line: what you'd buy (or the spread) and what it costs. */
export function PickLine({ pick }: { pick: Pick }) {
  if (isCreditSpread(pick)) {
    return (
      <>
        {pick.kind} ${strike(pick.short.strike)}/${strike(pick.long.strike)} {pick.short.expiration} for a {money(pick.credit, true)} credit (risk{" "}
        {money(pick.max_loss)})
      </>
    );
  }
  if (isCondor(pick)) {
    return (
      <>
        iron condor ${strike(pick.put_spread.long.strike)}/${strike(pick.put_spread.short.strike)}/${strike(pick.call_spread.short.strike)}/$
        {strike(pick.call_spread.long.strike)} {pick.put_spread.short.expiration} for a {money(pick.credit, true)} credit (risk {money(pick.max_loss)})
      </>
    );
  }
  if (isCalendar(pick)) {
    return (
      <>
        ${strike(pick.strike)} {pick.front.right} calendar {pick.front.expiration}/{pick.back.expiration} for {money(pick.debit, true)} ({money(pick.cost)})
      </>
    );
  }
  if (isSpread(pick)) {
    return (
      <>
        {pick.long.right} spread ${strike(pick.long.strike)}/${strike(pick.short.strike)} {pick.long.expiration} for{" "}
        {money(pick.debit, true)} ({money(pick.cost)})
      </>
    );
  }
  if (pick.cash_required != null) {
    return (
      <>
        sell {pick.label} for {money(pick.price, true)} ({money(pick.premium)}; {money(pick.cash_required)} set aside)
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
  if (isCreditSpread(pick)) return <CreditSpreadSummary s={pick} />;
  if (isCondor(pick)) return <CondorSummary c={pick} />;
  if (isCalendar(pick)) return <CalendarSummary c={pick} />;
  if (isSpread(pick)) return <SpreadSummary s={pick} />;
  if (pick.cash_required != null) return <CashSecuredPutSummary p={pick} />;
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

function CashSecuredPutSummary({ p }: { p: ContractPick }) {
  return (
    <div className="pick">
      <div className="pick-title">
        Sell <strong>{p.label}</strong> at {money(p.price, true)} <span className="muted">({money(p.premium)} collected, {money(p.cash_required)} set aside)</span>
      </div>
      <div className="pick-facts">
        <Fact label="Yield" value={pct(p.yield_pct, 1)} sub={`${pct(p.annualized_pct, 0)} a year`} />
        <Fact label="If assigned" value={money(p.net_entry, true)} sub={`${pct(p.net_entry_discount_pct, 1, true)} vs now`} />
        <Fact label="Odds assigned" value={pct(p.prob_assigned, 0)} sub={`${p.dte} days`} />
        <Fact label="Decay earned" value={`${signedMoney(-p.theta_day)}/day`} sub={`IV ${pct(p.iv, 0)}`} />
      </div>
    </div>
  );
}

function CreditSpreadSummary({ s }: { s: CreditSpreadPick }) {
  return (
    <div className="pick">
      <div className="pick-title">
        Sell <strong>{s.short.label}</strong>, buy the ${strike(s.long.strike)} for a {money(s.credit, true)} credit{" "}
        <span className="muted">({money(s.premium)} a spread)</span>
      </div>
      <div className="pick-facts">
        <Fact label="Max loss" value={money(s.max_loss)} sub={`${pct(s.return_on_risk, 0)} return on it`} />
        <Fact label="Breakeven" value={money(s.breakeven, true)} sub={pct(s.breakeven_move_pct, 1, true)} />
        <Fact label="Odds of profit" value={pct(s.prob_profit, 0)} sub="at expiry" />
        <Fact label="Decay earned" value={`${signedMoney(s.theta_day)}/day`} sub={`${s.dte} days`} />
      </div>
    </div>
  );
}

function CondorSummary({ c }: { c: IronCondorPick }) {
  return (
    <div className="pick">
      <div className="pick-title">
        Sell the <strong>${strike(c.put_spread.short.strike)} put and ${strike(c.call_spread.short.strike)} call</strong>, {c.put_spread.short.expiration}, wings at $
        {strike(c.put_spread.long.strike)}/${strike(c.call_spread.long.strike)}, for a {money(c.credit, true)} credit{" "}
        <span className="muted">({money(c.premium)})</span>
      </div>
      <div className="pick-facts">
        <Fact label="Max loss" value={money(c.max_loss)} sub={`${pct(c.return_on_risk, 0)} return on it`} />
        <Fact
          label="Profit range"
          value={`${money(c.lower_breakeven, true)}–${money(c.upper_breakeven, true)}`}
          sub={`${pct(c.range_pct[0], 1, true)} to ${pct(c.range_pct[1], 1, true)}`}
        />
        <Fact label="Odds of profit" value={pct(c.prob_profit, 0)} sub="at expiry" />
        <Fact label="Decay earned" value={`${signedMoney(c.theta_day)}/day`} sub={`${c.dte} days`} />
      </div>
    </div>
  );
}

function CalendarSummary({ c }: { c: CalendarPick }) {
  const [lo, hi] = c.profit_range;
  return (
    <div className="pick">
      <div className="pick-title">
        Sell <strong>{c.front.label}</strong>, buy the {c.back.expiration} ${strike(c.strike)} {c.back.right} for {money(c.debit, true)}{" "}
        <span className="muted">({money(c.cost)})</span>
      </div>
      <div className="pick-facts">
        <Fact label="Best case" value={money(c.max_profit_est)} sub="at the strike when the front expires" />
        <Fact label="Profit range" value={lo != null && hi != null ? `${money(lo, true)}–${money(hi, true)}` : "—"} sub={`odds ${pct(c.prob_profit, 0)}`} />
        <Fact label="Decay earned" value={`${signedMoney(c.theta_day)}/day`} sub={`vega ${signedMoney(c.vega)}/pt`} />
        <Fact label="IV" value={`${pct(c.front_iv, 0)} / ${pct(c.back_iv, 0)}`} sub="front / back" />
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
              : `${v.rank != null ? `rank ${pct(v.rank, 0)} · ` : ""}${v.iv_rv != null ? `${num(v.iv_rv, 1)}x the ${pct(v.rv_forecast, 0)} forecast · ` : ""}${v.iv_hv != null ? `${num(v.iv_hv, 1)}x realized ${pct(v.hv20, 0)}` : ""}${v.rich ? " · expensive" : ""}${
                  v.skew_25d != null ? ` · put skew ${num(-v.skew_25d * 100, 1)} pts` : ""
                }${v.term_shape ? ` · ${v.term_shape}` : ""}`
          }
        />
      </div>
      {data.fundamentals && <EarningsCard f={data.fundamentals} />}
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
      ) : null}
      {p.cash_secured_put || p.put_credit_spread || p.iron_condor || p.calendar ? (
        <>
          <p className="muted small" style={{ margin: 0 }}>
            Selling premium and time spreads{v.rich ? ": options are expensive here, which is when the seller's side pays" : ": options aren't expensive here, so the premium is thin"}.
          </p>
          <div className="grid cols-2 top">
            {p.cash_secured_put && <PickCard title="Cash-secured put" hint="paid to wait for a lower entry" pick={p.cash_secured_put} />}
            {p.put_credit_spread && <PickCard title="Put credit spread" hint="bullish; loss capped at the width" pick={p.put_credit_spread} />}
            {p.call_credit_spread && <PickCard title="Call credit spread" hint="bearish; loss capped at the width" pick={p.call_credit_spread} />}
            {p.iron_condor && <PickCard title="Iron condor" hint="paid while it stays in a range" pick={p.iron_condor} />}
            {p.calendar && <PickCard title="Calendar" hint="paid by the front month's faster decay" pick={p.calendar} />}
          </div>
        </>
      ) : null}
      {Object.keys(p).length ? null : (
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
          `Is selling premium on ${symbol} worth it here (cash-secured put or credit spread)?`,
          `Any earnings or news before these expiries?`,
        ]}
      />
      <PriceHistory symbol={symbol} live={live} />
      <OptionPricing underlying={symbol} />
      <p className="muted small">{data.disclaimer}</p>
    </div>
  );
}

export function EarningsCard({ f }: { f: Fundamentals }) {
  return (
    <Card title="Earnings and valuation" hint={`SEC filings, quarter to ${f.period_end ?? "—"}`}>
      <div className="metrics">
        <Fact label="Next results (estimate)" value={f.next_release_estimate ?? "—"} sub={f.last_release ? `last: ${f.last_release}` : null} />
        <Fact
          label="Last surprise"
          value={f.sue != null ? `${f.sue >= 0 ? "+" : "−"}${num(Math.abs(f.sue), 1)}σ` : "—"}
          sub={f.eps_last_quarter != null ? `EPS ${money(f.eps_last_quarter, true)} for the quarter` : null}
        />
        <Fact label="P/E" value={f.pe != null ? num(f.pe, 1) : "—"} sub={f.eps_ttm != null ? `trailing EPS ${money(f.eps_ttm, true)}` : null} />
        <Fact label="Book to market" value={f.book_to_market != null ? num(f.book_to_market, 2) : "—"} sub={f.roe != null ? `ROE ${pct(f.roe, 0)}` : null} />
        <Fact label="Gross profit / assets" value={pct(f.gross_profitability, 0)} sub="quality (Novy-Marx)" />
      </div>
    </Card>
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
