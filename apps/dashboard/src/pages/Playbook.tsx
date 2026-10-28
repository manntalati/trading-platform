import type { ReactNode } from "react";
import { money, num, pct, signedMoney } from "../format";
import {
  isPlay,
  isShares,
  type Budget,
  type HoldingAlert,
  type ModelRankInfo,
  type NewsContext,
  type OutOfReach,
  type PlayPick,
  type ResearchIdea,
  type StockPick,
  type VolContext,
  type WatchItem,
} from "../types";

/** One playbook idea: the setup that fired, the position that expresses it best under the volatility
 * forecast, what the model expects of it, and how to get in and out. */
export function IdeaCard({ idea }: { idea: ResearchIdea }) {
  const pick = isPlay(idea.pick) ? idea.pick : null;
  const shares = isShares(idea.pick) ? idea.pick : null;
  const alternative = isPlay(idea.alternative) ? idea.alternative : null;
  const tone = idea.kind === "bearish" ? "attention" : idea.kind === "income" ? "consider" : "info";
  return (
    <article className={`card idea ${tone}`}>
      <div className="idea-head">
        <a href={`#/research/lookup/${encodeURIComponent(idea.symbol)}`} className="idea-symbol">
          {idea.symbol}
        </a>
        {idea.new && <span className="pill new">new</span>}
        <span className="muted">{money(idea.spot, true)}</span>
        <span className="spacer" />
        {pick && (
          <span className={`small ${pick.model.expected_return > 0 ? "up" : "down"}`} title="modeled return on the capital at risk at the horizon">
            {pct(pick.model.expected_return, 0, true)} expected
          </span>
        )}
        {shares && (
          <span className={`small ${shares.model.drift > 0 ? "up" : "down"}`} title="the setup's drift over its horizon">
            {pct(shares.model.expected_return, 1, true)} drift
          </span>
        )}
      </div>
      <div className="tags">
        {idea.play && (
          <span className="pill setup" title={idea.play.rule}>
            {idea.play.title} · {idea.play.age === 0 ? "today" : `${idea.play.age}d ago`}
          </span>
        )}
        {idea.setup.tags.map((t) => (
          <span key={t} className="pill">
            {t}
          </span>
        ))}
        <VolTag vol={idea.vol} />
      </div>
      {pick ? <PlaySummary pick={pick} /> : shares ? <SharesSummary pick={shares} /> : <p className="muted small">No contract picked.</p>}
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
      {idea.news && <NewsLine news={idea.news} />}
      {alternative && (
        <p className="small muted">
          Alternative: <PlayLine pick={alternative} />
        </p>
      )}
      <a className="small" href={`#/research/lookup/${encodeURIComponent(idea.symbol)}`}>
        {idea.symbol} in depth, scenarios and Ask Claude →
      </a>
    </article>
  );
}

export function VolTag({ vol }: { vol: VolContext }) {
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

function legText(pick: PlayPick): string {
  const [first, ...rest] = pick.legs;
  if (!first) return pick.structure;
  if (!rest.length) return first.label;
  return `${first.label.replace(/ (Call|Put)$/, "")}/${rest.map((l) => `$${strike(l.strike)}`).join("/")} ${first.right}s`;
}

/** One line: the structure and what it costs or brings in. */
export function PlayLine({ pick }: { pick: PlayPick }) {
  const credit = pick.credit != null;
  return (
    <>
      {pick.structure}: {legText(pick)} for a {money(Math.abs(pick.mid), true)} {credit ? "credit" : "debit"} (risk {money(pick.max_loss)}),{" "}
      {pct(pick.model.expected_return, 0, true)} expected
    </>
  );
}

export function PlaySummary({ pick }: { pick: PlayPick }) {
  const credit = pick.credit != null;
  const g = pick.greeks;
  const m = pick.model;
  return (
    <div className="pick">
      <div className="pick-title">
        <strong>{pick.structure}</strong>{" "}
        <span className="muted">
          {pick.expiration} ({pick.dte} days)
        </span>
        {pick.legs.map((l) => (
          <div key={l.contract} className="leg">
            {l.side === "sell" ? "Sell" : "Buy"} {l.label} at {money(l.price, true)}{" "}
            <span className="muted">
              (bid {money(l.bid, true)} / ask {money(l.ask, true)}, delta {num(l.delta, 2)})
            </span>
          </div>
        ))}
      </div>
      <div className="pick-facts">
        <Fact
          label={`Expected, ${m.horizon} sessions`}
          value={<span className={m.expected_return > 0 ? "up" : "down"}>{pct(m.expected_return, 0, true)}</span>}
          sub={`odds of a profit ${pct(m.prob_profit, 0)}`}
        />
        <Fact label="1 in 10 worse than" value={pct(m.q10, 0, true)} sub={`max loss ${money(pick.max_loss)}`} />
        <Fact
          label={credit ? "Credit" : "Cost"}
          value={money(Math.abs(pick.cost))}
          sub={pick.max_gain != null ? `max gain ${money(pick.max_gain)}` : `${pick.legs.length > 1 ? "net" : "a contract"}`}
        />
        <Fact label="Delta" value={`${num(g.delta, 0)} sh`} sub={`gamma ${num(g.gamma, 2)}/$1`} />
        <Fact label="Theta" value={`${signedMoney(g.theta_day)}/day`} sub={g.theta_pct != null ? `${pct(g.theta_pct, 1, true)} of it a day` : null} />
        <Fact label="Vega" value={`${signedMoney(g.vega)}/pt`} sub={`model vol ${pct(m.vol, 0)}`} />
        {pick.breakeven_move != null && (
          <Fact
            label="Pays its decay at"
            value={`${pct(pick.breakeven_move, 1)}/day`}
            sub={pick.forecast_move != null ? `forecast ${pct(pick.forecast_move, 1)}/day` : null}
          />
        )}
        {pick.size && (
          <Fact
            label="Size"
            value={
              <span className={pick.size.over_guideline ? "down" : undefined}>
                {pick.size.contracts} contract{pick.size.contracts === 1 ? "" : "s"}
              </span>
            }
            sub={`${money(pick.size.capital)} · ${pct(pick.size.share_of_account, 1)} at risk`}
          />
        )}
      </div>
      <EntryExit pick={pick} />
    </div>
  );
}

/** A stock idea: shares at a limit, a stop and an optional target some ATRs away, a time stop, and the backtest of
 * the same trade on the setup's later signals. */
export function SharesSummary({ pick }: { pick: StockPick }) {
  const m = pick.model;
  const x = pick.exit;
  const e = pick.entry;
  const t = pick.backtest;
  return (
    <div className="pick">
      <div className="pick-title">
        <strong>Shares</strong> <span className="muted">at {money(e.limit, true)} or less</span>
      </div>
      <div className="pick-facts">
        <Fact
          label={`Drift, ${m.horizon} sessions`}
          value={<span className={m.drift > 0 ? "up" : "down"}>{pct(m.expected_return, 1, true)}</span>}
          sub={`volatility ${pct(m.vol, 0)}`}
        />
        <Fact label="Stop" value={money(x.stop, true)} sub={`${pct(-m.stop_pct, 1, true)} · ${num(x.rules.stop_atr, 1)} ATR`} />
        <Fact
          label="Target"
          value={x.target != null ? money(x.target, true) : "none"}
          sub={m.target_pct != null ? pct(m.target_pct, 1, true) : x.stock ? "the stock rule sells" : null}
        />
        {t && t.trades > 0 && (
          <Fact label="Backtest" value={`${pct(t.win_rate ?? null, 0)} won`} sub={`${pct(t.mean ?? null, 1, true)} in ${num(t.avg_sessions ?? null, 0)} sessions`} />
        )}
        {pick.size && (
          <Fact
            label="Size"
            value={
              <span className={pick.size.over_guideline ? "down" : undefined}>
                {pick.size.shares} share{pick.size.shares === 1 ? "" : "s"}
              </span>
            }
            sub={`${money(pick.size.capital)} · ${money(pick.size.max_loss)} to the stop`}
          />
        )}
      </div>
      <div className="plan">
        <div>
          <span className="plan-label">Entry</span> limit {money(e.limit, true)}; pay no more than <strong>{money(e.worst, true)}</strong>
          {e.valid_until ? <>, until {e.valid_until}</> : null}
        </div>
        <div>
          <span className="plan-label">Exit</span> stop at {money(x.stop, true)}
          {x.target != null && <>; take profit at {money(x.target, true)}</>}
          {x.stock && (
            <>
              ; or on {x.stock.text}
              {x.stock.level != null ? ` (now ${money(x.stock.level, true)})` : ""}
            </>
          )}
          ; sell by <strong>{x.sell_by ?? "—"}</strong>
        </div>
      </div>
    </div>
  );
}

function tone(score: number): string {
  return score >= 0.15 ? "up" : score <= -0.15 ? "down" : "muted";
}

function scoredBy(news: NewsContext): string {
  if (news.claude === news.headlines) return "scored by Claude";
  if (!news.claude) return "keyword scores";
  return `${news.claude} scored by Claude, the rest by keywords`;
}

/** A name's recent news on an idea: the mean sentiment, how many headlines, and the latest. */
export function NewsLine({ news }: { news: NewsContext }) {
  const latest = news.latest[0];
  return (
    <p className="small news-line">
      <span className="plan-label">News</span>{" "}
      <span className={tone(news.sentiment)}>{num(news.sentiment, 2)}</span>{" "}
      <span className="muted">
        from {news.headlines} headline{news.headlines === 1 ? "" : "s"} in {news.sessions} sessions ({scoredBy(news)})
      </span>
      {latest && (
        <>
          {" · "}
          {latest.url ? (
            <a href={latest.url} target="_blank" rel="noreferrer">
              {latest.headline}
            </a>
          ) : (
            latest.headline
          )}{" "}
          <span className="muted">
            {latest.source ? `${latest.source}, ` : ""}
            {new Date(latest.at).toLocaleDateString()}
          </span>
        </>
      )}
    </p>
  );
}

/** A name's latest headlines with their scores (Look up). */
export function NewsList({ news }: { news: NewsContext | null | undefined }) {
  if (!news) return <p className="muted small">No headlines naming it in the last five sessions.</p>;
  return (
    <div className="watch">
      <div className="small">
        Mean sentiment <strong className={tone(news.sentiment)}>{num(news.sentiment, 2)}</strong> from {news.headlines} headline
        {news.headlines === 1 ? "" : "s"} since {news.since} <span className="muted">({scoredBy(news)})</span>
      </div>
      {news.latest.map((h) => (
        <div key={`${h.at}${h.headline}`} className="watch-row">
          <span className={`mono ${tone(h.score)}`} title={h.scored_by === "claude" ? "Claude's score" : "keyword score"}>
            {num(h.score, 1)}
          </span>
          {h.url ? (
            <a href={h.url} target="_blank" rel="noreferrer" className="small">
              {h.headline}
            </a>
          ) : (
            <span className="small">{h.headline}</span>
          )}
          <span className="spacer" />
          <span className="muted small">
            {h.source ? `${h.source}, ` : ""}
            {new Date(h.at).toLocaleString()}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Downward setups live on stocks you hold: reasons to look at selling or tightening a stop. */
export function HoldingAlerts({ items }: { items: HoldingAlert[] }) {
  if (!items.length) return null;
  return (
    <div className="watch">
      <div className="small">
        <strong>On what you hold</strong> <span className="muted">· a downward setup fired, or bad news: worth a look at selling or a tighter stop</span>
      </div>
      {items.map((a) => (
        <a key={`${a.symbol}-${a.setup}`} className="watch-row" href={`#/research/lookup/${encodeURIComponent(a.symbol)}`}>
          <span className="mono">{a.symbol}</span>
          <span className="muted small">{a.held}</span>
          <span className="small">{a.text}</span>
        </a>
      ))}
    </div>
  );
}

/** Where the stock ideas' ML rank comes from, and whether it counts. */
export function ModelRankLine({ rank }: { rank: ModelRankInfo | null | undefined }) {
  if (!rank) {
    return <>The ML ranker hasn't scored the names yet (the scheduler's models job, or <code>tp-models monitor</code>), so it doesn't move the order. </>;
  }
  return (
    <>
      The ML ranker scored {rank.names} names on {rank.day}
      {rank.rank_ic != null ? ` (walk-forward rank IC ${num(rank.rank_ic, 3)})` : ""}:{" "}
      {rank.counted ? "its rank moves the order." : "shown, not counted, since its ranking hasn't held up out of sample."}{" "}
    </>
  );
}

/** How to get in (a limit, a ceiling, a deadline) and out (target, stop, the stock-side rule, the time limits). */
export function EntryExit({ pick }: { pick: PlayPick }) {
  const e = pick.entry;
  const x = pick.exit;
  const credit = e.side === "credit";
  const tp = x.rules.take_profit;
  const sl = x.rules.stop_loss;
  return (
    <div className="plan">
      <div>
        <span className="plan-label">Entry</span> limit {money(e.limit, true)} {credit ? "credit" : "debit"} (the mid);{" "}
        {credit ? "take no less than" : "pay no more than"} <strong>{money(e.worst, true)}</strong>
        {e.valid_until ? <>, until {e.valid_until}</> : null}
        {!e.worth_it && <span className="down"> · the mid is past it</span>}
      </div>
      <div>
        <span className="plan-label">Exit</span>
        {x.take_profit != null && tp != null && (
          <>
            {" "}
            take profit at {money(x.take_profit, true)} ({credit ? `${pct(tp, 0)} of the credit kept` : pct(tp, 0, true)});
          </>
        )}
        {x.stop != null && sl != null && (
          <>
            {" "}
            stop at {money(x.stop, true)} ({credit ? `losing ${num(-sl, 1)}x the credit` : pct(sl, 0, true)});
          </>
        )}
        {x.stock && (
          <>
            {" "}
            or on {x.stock.text}
            {x.stock.level != null ? ` (now ${money(x.stock.level, true)})` : ""};
          </>
        )}{" "}
        out by <strong>{x.sell_by ?? x.out_by}</strong>
        {x.sell_by && x.sell_by !== x.out_by ? <span className="muted"> (and before {x.out_by}, {x.rules.min_dte} days to expiry)</span> : null}
      </div>
    </div>
  );
}

/** Where the ideas' sizes come from: the account, its buying power and the risk guideline. */
export function BudgetLine({ budget }: { budget: Budget | null | undefined }) {
  if (!budget) {
    return (
      <>
        Not sized to an account yet: once the brokerage is synced (<code>tp-broker sync</code>), ideas only use contracts your buying power pays for.{" "}
      </>
    );
  }
  return (
    <>
      Sized for <strong>{budget.account}</strong>
      {budget.paper ? " (paper)" : ""}: buying power <strong>{money(budget.buying_power)}</strong>, at most {pct(budget.risk_per_trade, 0)} of{" "}
      {money(budget.account_value)} ({money(budget.risk_cap)}) at risk a trade
      {budget.options_level != null ? `, options level ${budget.options_level}` : ""}
      {budget.as_of ? ` (synced ${new Date(budget.as_of).toLocaleString()})` : ""}.{" "}
    </>
  );
}

/** Setups that fired where nothing the account can trade fits. */
export function OutOfReachList({ items, budget }: { items: OutOfReach[]; budget: Budget | null | undefined }) {
  if (!items.length) return null;
  return (
    <p className="muted small">
      Out of reach{budget ? ` with ${money(budget.buying_power)} of buying power` : ""}:{" "}
      {items.map((m, i) => (
        <span key={`${m.symbol}-${m.setup}`}>
          {i > 0 && "; "}
          <a href={`#/research/lookup/${encodeURIComponent(m.symbol)}`}>{m.symbol}</a> {m.setup.toLowerCase()} (
          {m.structure === "shares"
            ? `a share costs ${money(m.needs, true)}`
            : m.reason === "buying power"
              ? `the cheapest fit, a ${m.structure}, needs ${money(m.needs)}`
              : `needs options level ${m.level}`}
          )
        </span>
      ))}
      .
    </p>
  );
}

export function WatchList({ items }: { items: WatchItem[] }) {
  if (!items.length) return null;
  return (
    <div className="watch">
      <div className="small">
        <strong>Leaders to wait for</strong> <span className="muted">· a LEAPS entry comes when they pull back to the 50-day average</span>
      </div>
      {items.map((w) => (
        <a key={w.symbol} className="watch-row" href={`#/research/lookup/${encodeURIComponent(w.symbol)}`}>
          <span className="mono">{w.symbol}</span>
          <span className="muted small">
            {money(w.spot, true)} · {pct(w.above_ma50, 1, true)} vs the 50-day · momentum {pct(w.momentum, 0, true)}
          </span>
          <span className="spacer" />
          <span className="small">enter near {money(w.entry_level, true)}</span>
        </a>
      ))}
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
