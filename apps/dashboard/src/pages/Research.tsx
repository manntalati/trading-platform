import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, getJson, postJson, useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, go, Loading, Stat, SubNav } from "../components/ui";
import { money, num, nyTime, pct, qty, signedMoney } from "../format";
import type { LiveState } from "../live";
import {
  isCalendar,
  isCondor,
  isCreditSpread,
  isPlay,
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
  type PlayPick,
  type Research as ResearchData,
  type SpreadPick,
  type SymbolResearch,
  type Calibration,
  type TrackRecord,
  type TrackedRecommendation,
} from "../types";
import Ideas from "./Ideas";
import { BudgetLine, IdeaCard, OutOfReachList, PlayLine, WatchList } from "./Playbook";
import { PriceHistory } from "./Market";
import { OptionPricing } from "./Options";
import { AskPanel, Scenarios } from "./Positions";

export { IdeaCard } from "./Playbook";

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
    rule: "Calls or call spreads on setups that just fired: a pullback in an uptrend (RSI(2) under 10), a breakout from a squeeze, a strong earnings surprise. Each contract (expiry, delta, a single call or a spread) is chosen by its modeled return over the setup's horizon under the stock's volatility forecast, so cheap options favour calls and expensive ones spreads.",
  },
  {
    key: "leaps",
    label: "LEAPS",
    rule: "Trend leaders (12-1 month momentum, near their highs) pulling back to the 50-day average: a LEAPS call 9+ months out at delta 0.70-0.85, held for months. Leaders still extended are listed to wait for, with the entry level.",
  },
  {
    key: "bearish",
    label: "Bearish puts",
    rule: "Puts or put spreads on a bounce failing in a downtrend (RSI(2) over 90), a breakdown to a new 50-day low, or an earnings miss. Puts start with the market's upward drift against them; the model shows it.",
  },
  {
    key: "income",
    label: "Selling premium",
    rule: "Options at least 1.25x the volatility forecast (IV rank 50%+ when known) with no event priced in and no downtrend: put credit spreads, cash-secured puts, or an iron condor without a trend, valued at 21 days to expiry. The gain is capped at the premium; size for the maximum loss.",
  },
  {
    key: "volatility",
    label: "Cheap volatility",
    rule: "Options at most 0.9x the volatility forecast, the term structure not inverted and no event priced in: a straddle that profits from movement either way.",
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
  const ideas = r[kind] ?? []; // runs stored before a list existed lack it
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
        <BudgetLine budget={r.budget} />
        {r.calibration ? (
          <>
            Setups calibrated on {r.calibration.start ?? "the bars"} to {r.calibration.end ?? "now"} ({r.calibration.generated_at.slice(0, 10)}):
            drifts and exits from the backtest.{" "}
          </>
        ) : (
          <>
            The setups aren't calibrated yet, so the model assumes an ordinary market drift and the default exits: run <code>tp-recs calibrate</code> (the
            scheduler does weekly).{" "}
          </>
        )}
        {r.scanned} names scanned, {r.with_chains} with option chains
        {r.chains_as_of ? ` (as of ${new Date(r.chains_as_of).toLocaleString()})` : ""}. IV rank needs 20 days of daily snapshots
        {r.iv_history_days < 20 ? `: ${r.iv_history_days} so far, so implied vs realized volatility stands in.` : "."}{" "}
        Add your own tickers in <code>config/universes.local.toml</code>, or <a href="#/research/lookup">look one up</a> to fetch it now.
      </div>
      <div className="toolbar">
        {KINDS.map((k) => (
          <button key={k.key} aria-pressed={kind === k.key} onClick={() => setKind(k.key)}>
            {k.label} ({(r[k.key] ?? []).length}
            {(r[k.key] ?? []).some((i) => i.new) ? `, ${(r[k.key] ?? []).filter((i) => i.new).length} new` : ""})
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
          <Empty>No {info.label.toLowerCase()} setups right now: none of the rules has fired on a name with a liquid contract.</Empty>
        </Card>
      )}
      {kind === "leaps" && <WatchList items={r.watch ?? []} />}
      <OutOfReachList items={(r.out_of_reach ?? []).filter((m) => m.list === kind)} budget={r.budget} />
      {dropped.length > 0 && <p className="muted small">No longer listed since the previous session: {dropped.join(", ")}.</p>}
      {Object.keys(r.errors ?? {}).length > 0 && (
        <p className="muted small">
          Skipped because of an error (details in the tp-api log): {Object.keys(r.errors ?? {}).join(", ")}.
        </p>
      )}
      {r.without_chains.length > 0 && (
        <p className="muted small">
          Scanned without option chains (no contract to pick): {r.without_chains.join(", ")}. They get chains once they're in your research list.
        </p>
      )}
      <p className="muted small">{r.disclaimer}</p>
    </div>
  );
}

export function newCount(r: ResearchData): number {
  return KINDS.reduce((n, k) => n + (r[k.key] ?? []).filter((i) => i.new).length, 0);
}

// -- how past recommendations did -----------------------------------------------------------------

const PAGE = 15; // ideas a page in the track record

const LIST_LABEL: Record<IdeaList, string> = { swing: "Swing", leaps: "LEAPS", bearish: "Bearish", income: "Premium", volatility: "Volatility" };

function Followed({ r }: { r: TrackedRecommendation }) {
  const f = r.followed;
  if (!f) return <span className="muted">—</span>;
  return (
    <>
      {f.result != null ? <Delta value={f.result} digits={0} /> : "—"}
      <div className="muted small">{f.status === "closed" ? `${f.reason} · ${f.exit}` : `open · ${f.sessions} session${f.sessions === 1 ? "" : "s"}`}</div>
    </>
  );
}

/** The playbook backtest, per setup: how often the stock went its way, and the option trade on the
 * signals the exits weren't chosen on. */
export function BacktestCard({ c }: { c: Calibration }) {
  const rows = Object.entries(c.setups);
  if (!rows.length) {
    return (
      <Card title="Backtest by setup">
        <Empty>
          Not calibrated yet: run <code>tp-recs calibrate</code> (the scheduler does on Saturdays).
        </Empty>
      </Card>
    );
  }
  return (
    <Card title="Backtest by setup" hint={c.generated_at ? `${c.names} names, ${c.start} to ${c.end} · ${c.generated_at.slice(0, 10)}` : undefined}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Setup</th>
              <th className="num">Signals</th>
              <th className="num" title="the stock went the setup's way over its horizon">Stock right way</th>
              <th className="num" title="the stock's mean log return over the horizon, shrunk toward all names': what the live model assumes">Drift</th>
              <th className="num" title="the option trade with the chosen exits, on the later 40% of the signals">Option trades</th>
              <th className="num">Won</th>
              <th className="num">Mean</th>
              <th className="num" title="the same signals with the default exits">Default exits</th>
              <th className="num" title="on real option prices, since February 2024">Real prices</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, s]) => (
              <tr key={name}>
                <td>
                  {s.title}
                  {s.exits && (
                    <div className="muted small">
                      exits: {s.exits.take_profit != null ? `+${pct(s.exits.take_profit, 0)}` : "no target"} ·{" "}
                      {s.exits.stop_loss != null ? pct(s.exits.stop_loss, 0, true) : "no stop"} · {s.exits.time_stop} sessions
                      {s.exits.underlying ? ` · ${s.exits.underlying.replace("_", " ")}` : ""}
                    </div>
                  )}
                </td>
                <td className="num mono">{s.events}</td>
                <td className="num mono">{pct(s.underlying?.hit_rate ?? null, 0)}</td>
                <td className="num mono">
                  {pct(s.drift, 1, true)} <span className="muted small">/ {s.horizon}d</span>
                </td>
                <td className="num mono">{s.option?.trades ?? 0}</td>
                <td className="num mono">{pct(s.option?.win_rate ?? null, 0)}</td>
                <td className="num mono">{pct(s.option?.mean ?? null, 0, true)}</td>
                <td className="num mono">{pct(s.option?.detail?.default?.mean ?? null, 0, true)}</td>
                <td className="num mono">
                  {s.real?.trades ? (
                    <>
                      {pct(s.real.mean ?? null, 0, true)} <span className="muted small">of {s.real.trades}</span>
                    </>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {c.model && <p className="muted small">Modeled option prices: {c.model}. Real prices replace them where fetched (tp-data options history).</p>}
    </Card>
  );
}

function TrackRecordView() {
  const res = useApi<TrackRecord>("/api/research/track-record", 600_000);
  const calibration = useApi<Calibration>("/api/research/calibration", 3_600_000);
  const t = res.data;
  if (res.loading && !t) return <Loading />;
  if (!t) return <ErrorBanner error={res.error} />;
  if (!t.recommendations.length) {
    return (
      <div className="grid">
        <Card>
          <Empty>No recommendations stored yet. The job scheduler stores every run from now on; their track record builds up here.</Empty>
        </Card>
        {calibration.data && <BacktestCard c={calibration.data} />}
      </div>
    );
  }
  return (
    <div className="grid">
      <ErrorBanner error={res.error} />
      <p className="muted small" style={{ margin: 0 }}>
        Each idea once: a name on a list for one setup, tracked with the contract the first run picked, as if bought then. Runs come many times a day and the
        contract shifts with prices; listed again within a week, it's still the same idea. <strong>Stock</strong> is the move since in the direction
        recommended; <strong>Position</strong> marks the contracts at the middle of the latest bid and ask, or at expiry (premium selling: the share of the
        premium kept). <strong>Following the plan</strong> closes each at the first session its exit plan said to. Rules applied to data, not trades anyone made.
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
                <th className="num" title="following each idea's exit plan: closed ones that made money">Plan: won</th>
                <th className="num">Plan: median</th>
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
                  <td className="num mono">
                    {pct(s.plan_win_rate ?? null, 0)} <span className="muted">of {s.plan_closed ?? 0}</span>
                  </td>
                  <td className="num mono">{pct(s.plan_median ?? null, 0, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <TrackedIdeas t={t} />
      {t.by_setup && t.by_setup.length > 0 && (
        <Card title="By setup, following the exit plans">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Setup</th>
                  <th className="num">Ideas</th>
                  <th className="num">Closed by the plan</th>
                  <th className="num">Won</th>
                  <th className="num">Median</th>
                </tr>
              </thead>
              <tbody>
                {t.by_setup.map((s) => (
                  <tr key={s.setup}>
                    <td>{s.setup.replace("_", " ")}</td>
                    <td className="num mono">{s.recommendations}</td>
                    <td className="num mono">{s.plan_closed}</td>
                    <td className="num mono">{pct(s.plan_win_rate, 0)}</td>
                    <td className="num mono">{pct(s.plan_median, 0, true)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {calibration.data && <BacktestCard c={calibration.data} />}
    </div>
  );
}

type State = "all" | "open" | "closed";

function runsTitle(r: TrackedRecommendation): string {
  const contracts = (r.contracts ?? 1) > 1 ? `, with ${r.contracts} different contracts; tracked with the first` : "";
  return `listed in ${r.runs} runs until ${r.last_seen.slice(0, 10)}${contracts}`;
}

/** Each tracked idea, newest first, a page at a time, by list and by whether it's still open. */
export function TrackedIdeas({ t }: { t: TrackRecord }) {
  const [list, setList] = useState<IdeaList | "all">("all");
  const [state, setState] = useState<State>("all");
  const [shown, setShown] = useState(PAGE);
  const matching = t.recommendations.filter((r) => (list === "all" || r.list === list) && (state === "all" || (r.state ?? "open") === state));
  const rows = matching.slice(0, shown);
  const open = t.recommendations.filter((r) => (r.state ?? "open") === "open").length;
  function pick<T>(set: (value: T) => void, value: T) {
    set(value);
    setShown(PAGE);
  }
  return (
    <Card
      title="Each idea"
      hint={
        <span className="row-gap">
          <span className="muted small">{`${t.total ?? t.recommendations.length} ideas, ${open} open`}</span>
          <select aria-label="State" value={state} onChange={(e) => pick(setState, e.target.value as State)}>
            <option value="all">Open and closed</option>
            <option value="open">Open</option>
            <option value="closed">Closed</option>
          </select>
          <select aria-label="List" value={list} onChange={(e) => pick(setList, e.target.value as IdeaList | "all")}>
            <option value="all">All lists</option>
            {t.summary.map((s) => (
              <option key={s.list} value={s.list}>
                {s.name}
              </option>
            ))}
          </select>
        </span>
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
              <th className="num" title="what following the exit plan made, and why it closed">Following the plan</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.list}${r.symbol}${r.recommended_at}${r.legs.map((l) => l.contract).join()}`}>
                <td className="mono small">{r.recommended_at.slice(0, 10)}</td>
                <td>
                  <a href={`#/research/lookup/${encodeURIComponent(r.symbol)}`}>{r.symbol}</a> <span className="muted small">{LIST_LABEL[r.list]}</span>
                  {r.setup && <div className="muted small">{r.setup.replace("_", " ")}</div>}
                  {r.runs > 1 && (
                    <span className="muted small" title={runsTitle(r)}>
                      {" "}
                      · {r.runs} runs{r.last_seen.slice(0, 10) !== r.recommended_at.slice(0, 10) ? ` to ${r.last_seen.slice(5, 10)}` : ""}
                    </span>
                  )}
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
                <td className="num mono">
                  <Followed r={r} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!matching.length && <Empty>No {state === "all" ? "" : `${state} `}ideas on this list.</Empty>}
      {matching.length > rows.length && (
        <button className="more" onClick={() => setShown(shown + PAGE)}>
          Show {Math.min(PAGE, matching.length - rows.length)} more of {matching.length - rows.length}
        </button>
      )}
    </Card>
  );
}

function strike(k: number): string {
  return k.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** One line: what you'd buy (or the spread) and what it costs. */
export function PickLine({ pick }: { pick: Pick | PlayPick }) {
  if (isPlay(pick)) return <PlayLine pick={pick} />;
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
        <Stat label="Trend" value={data.setup.direction} sub={data.setup.tags.join(" · ") || "between its averages"} />
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
      {data.playbook && <PlaybookCard symbol={symbol} view={data.playbook} />}
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

function PlaybookCard({ symbol, view }: { symbol: string; view: NonNullable<SymbolResearch["playbook"]> }) {
  return (
    <Card title="The playbook" hint={view.calibrated ? "calibrated by the backtest" : "not calibrated yet: tp-recs calibrate"}>
      {view.budget !== undefined && (
        <p className="muted small" style={{ marginTop: 0 }}>
          <BudgetLine budget={view.budget} />
        </p>
      )}
      {view.ideas.length ? (
        <div className="grid cols-2 top">
          {view.ideas.map((i) => (
            <IdeaCard key={`${i.kind}-${i.play?.name ?? ""}`} idea={i} />
          ))}
        </div>
      ) : (
        <p className="muted" style={{ marginTop: 0 }}>
          No entry setup is live on {symbol} today.
          {view.watch ? " It's a trend leader: a LEAPS entry comes on a pullback to its 50-day average." : ""}
        </p>
      )}
      <OutOfReachList items={view.out_of_reach ?? []} budget={view.budget} />
      <ul className="why">
        {view.levels.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </Card>
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
