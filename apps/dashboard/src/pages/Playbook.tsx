import type { ReactNode } from "react";
import { money, num, pct, signedMoney } from "../format";
import { isPlay, type PlayPick, type ResearchIdea, type VolContext, type WatchItem } from "../types";

/** One playbook idea: the setup that fired, the position that expresses it best under the volatility
 * forecast, what the model expects of it, and how to get in and out. */
export function IdeaCard({ idea }: { idea: ResearchIdea }) {
  const pick = isPlay(idea.pick) ? idea.pick : null;
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
      {pick ? <PlaySummary pick={pick} /> : <p className="muted small">No contract picked.</p>}
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
      </div>
      <EntryExit pick={pick} />
    </div>
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
