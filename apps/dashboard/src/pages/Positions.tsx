import { useEffect, useRef, useState, type ReactNode } from "react";
import { postJson } from "../api";
import { Card, Delta, Empty } from "../components/ui";
import { money, num, pct, qty, signedMoney, timeAgo } from "../format";
import {
  isOption,
  type AskReply,
  type HoldingAction,
  type CashSummary,
  type ChatTurn,
  type OptionPositionRow,
  type PositionNote,
  type PositionRow,
  type Positions as PositionsData,
  type ScenarioGrid,
  type StockPosition,
} from "../types";

/** Every open position: options first (soonest expiry), then stocks and funds, and one opened in
 * full (``#/portfolio/positions/<symbol>``): statistics, notes, what you could do, scenarios,
 * and Claude to talk it through. The Portfolio section fetches the data. */
export default function PositionsView(props: {
  data: PositionsData;
  selected: string | null;
  onSelect: (symbol: string | null) => void;
}) {
  const { data: p, selected, onSelect } = props;
  const [threads, setThreads] = useState<Record<string, ChatTurn[]>>({});
  const options = p.positions.filter(isOption).sort((a, b) => a.stats.days_left - b.stats.days_left);
  const others = p.positions.filter((x): x is StockPosition => !isOption(x));
  const current = p.positions.find((x) => x.symbol === selected) ?? null;
  return (
    <div className="grid">
      <QuotesNote market={p.market_data} />
      {current && (
        <PositionDetail
          key={current.symbol}
          position={current}
          ask={p.ask}
          thread={threads[current.symbol] ?? []}
          onThread={(turns) => setThreads((all) => ({ ...all, [current.symbol]: turns }))}
          onClose={() => onSelect(null)}
        />
      )}

      {options.length > 0 && (
        <Card title="Options" hint="soonest expiry first · click one for the full picture and what you could do">
          <OptionsTable rows={options} selected={selected} onSelect={onSelect} />
        </Card>
      )}

      <Card title="Stocks and funds" hint="click one for its statistics and what you could do">
        <StocksTable rows={others} selected={selected} onSelect={onSelect} />
      </Card>

      {!current && (
        <AskPanel
          symbol={null}
          title="Ask Claude about the whole account"
          ask={p.ask}
          thread={threads[""] ?? []}
          onThread={(turns) => setThreads((all) => ({ ...all, "": turns }))}
          suggestions={["Which positions carry the most risk right now?", "Which options should I look at first, and why?"]}
        />
      )}
    </div>
  );
}

function QuotesNote({ market }: { market: PositionsData["market_data"] }) {
  if (!market || market.options === 0) return null;
  if (market.error) {
    return <div className="banner error">Live option quotes failed ({market.error.slice(0, 160)}); showing the broker's last prices.</div>;
  }
  if (!market.source) {
    return (
      <div className="banner">
        Option prices are the broker's last ones (can be a day old). For live bid/ask, start the API with live quotes:{" "}
        <code>uv run tp-api</code> with Alpaca keys in <code>.env</code>.
      </div>
    );
  }
  return null;
}

/** Brokers count a purchase in the account total right away but take the cash only when the
 * trade settles; the difference would otherwise show up as cash you don't have. */
export function CashNote({ cash }: { cash: CashSummary }) {
  if (Math.abs(cash.unsettled) < 1) return null;
  const buying = cash.unsettled > 0;
  return (
    <div className="banner">
      Cash is {money(cash.cash)}: your broker's cash balance shows {money(cash.reported_cash)}, but {money(Math.abs(cash.unsettled))}{" "}
      {buying ? "of it is paying for trades that haven't settled yet" : "from sales hasn't settled into it yet"} (trades settle the next business day).
      {cash.buying_power != null && <> Buying power {money(cash.buying_power)}.</>}
    </div>
  );
}

function OptionsTable({ rows, selected, onSelect }: { rows: OptionPositionRow[]; selected: string | null; onSelect: (s: string) => void }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Contract</th>
            <th className="num">Qty</th>
            <th className="num">Mark</th>
            <th className="num">Value</th>
            <th className="num">P&L</th>
            <th className="num">Underlying</th>
            <th className="num">Needs</th>
            <th className="num">Decay / day</th>
            <th className="num">Odds of profit</th>
            <th>Expires</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((o) => {
            const s = o.stats;
            const days = Math.max(s.days_left, 0);
            return (
              <tr key={o.symbol} className={`clickable ${selected === o.symbol ? "selected" : ""}`} onClick={() => onSelect(o.symbol)}>
                <td>
                  <button className="link strong" onClick={() => onSelect(o.symbol)}>
                    {o.label}
                  </button>
                  {o.notes.some((n) => n.level === "risk") ? (
                    <span className="source-tag risk">act before expiry</span>
                  ) : (o.actions?.length ?? 0) > 0 ? (
                    <span className="source-tag">{o.actions!.length} to consider</span>
                  ) : null}
                </td>
                <td className="num">{qty(o.quantity)}</td>
                <td className="num" title={`from ${o.price_source}`}>
                  {money(o.price, true)}
                  <div className="small muted">{o.price_source}</div>
                </td>
                <td className="num">{money(o.market_value)}</td>
                <td className="num">
                  <Delta value={o.pnl} kind="money" />
                  <div className="small">
                    <Delta value={o.pnl_pct} digits={0} />
                  </div>
                </td>
                <td className="num">
                  {money(s.spot, true)}
                  <div className="small muted">{s.moneyness == null ? "" : `${s.moneyness >= 0 ? "ITM" : "OTM"} ${pct(Math.abs(s.moneyness), 1)}`}</div>
                </td>
                <td className="num">{needs(s.breakeven_move_pct, s.right, o.quantity)}</td>
                <td className="num">{s.theta_day != null ? signedMoney(s.theta_day) : "—"}</td>
                <td className="num">{pct(s.prob_profit, 0)}</td>
                <td>
                  {s.expiration}
                  <div className="small">
                    {days < 1 ? (
                      <span>
                        <span className="dot critical" aria-hidden="true" /> today
                      </span>
                    ) : days <= 7 ? (
                      <span>
                        <span className="dot warning" aria-hidden="true" /> {days.toFixed(1)} days
                      </span>
                    ) : (
                      <span className="muted">{days.toFixed(0)} days</span>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function needs(move: number | null | undefined, right: "call" | "put", quantity: number): string {
  if (move == null) return "—";
  const passed = right === "call" ? move <= 0 : move >= 0;
  if (quantity < 0) return passed ? "at risk" : "safe so far";
  return passed ? "past breakeven" : pct(move, 1, true);
}

function StocksTable({ rows, selected, onSelect }: { rows: StockPosition[]; selected: string | null; onSelect: (s: string) => void }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Qty</th>
            <th className="num">Price</th>
            <th className="num">Today</th>
            <th className="num">Value</th>
            <th className="num">Weight</th>
            <th className="num">P&L</th>
            <th className="num">1M</th>
            <th className="num">From 52w high</th>
            <th className="num">Volatility</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const s = r.stats;
            return (
              <tr key={r.symbol} className={`clickable ${selected === r.symbol ? "selected" : ""}`} onClick={() => onSelect(r.symbol)}>
                <td title={r.description ?? undefined}>
                  <button className="link strong" onClick={() => onSelect(r.symbol)}>
                    {r.symbol}
                  </button>
                  {(r.actions?.length ?? 0) > 0 && <span className="source-tag">{r.actions!.length} to consider</span>}
                </td>
                <td className="num">{qty(r.quantity)}</td>
                <td className="num">{money(r.price, true)}</td>
                <td className="num">
                  <Delta value={s.day_change_pct} />
                </td>
                <td className="num">{money(r.market_value)}</td>
                <td className="num">{pct(r.weight)}</td>
                <td className="num">
                  <Delta value={r.pnl} kind="money" />
                  <div className="small">
                    <Delta value={r.pnl_pct} digits={1} />
                  </div>
                </td>
                <td className="num">
                  <Delta value={s.returns?.["1M"]} digits={1} />
                </td>
                <td className="num">{s.from_high_pct != null && s.from_high_pct > -0.005 ? "at the high" : pct(s.from_high_pct, 0)}</td>
                <td className="num">{pct(s.vol_20d, 0)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ExposureCard({ exposure }: { exposure: NonNullable<PositionsData["exposure"]> }) {
  return (
    <Card title="Exposure by underlying" hint="shares plus what the options behave like (delta), for names you hold options on">
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Underlying</th>
              <th className="num">Shares</th>
              <th className="num">Options act like</th>
              <th className="num">Net shares</th>
              <th className="num">Net exposure</th>
            </tr>
          </thead>
          <tbody>
            {exposure.map((e) => (
              <tr key={e.underlying}>
                <td>
                  <strong>{e.underlying}</strong>
                </td>
                <td className="num">{qty(e.shares)}</td>
                <td className="num">{num(e.option_delta_shares, 0)} shares</td>
                <td className="num">{num(e.net_shares, 0)}</td>
                <td className="num">{money(e.net_value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// -- one position -------------------------------------------------------------------------------

function PositionDetail(props: {
  position: PositionRow;
  ask: PositionsData["ask"];
  thread: ChatTurn[];
  onThread: (t: ChatTurn[]) => void;
  onClose: () => void;
}) {
  const p = props.position;
  const ref = useRef<HTMLElement>(null);
  useEffect(() => ref.current?.scrollIntoView?.({ block: "start", behavior: "smooth" }), []);
  const option = isOption(p);
  return (
    <section ref={ref} className="grid" aria-label={`${p.label} details`}>
      <Card
        title={
          <>
            <strong className="detail-title">{p.label}</strong>
            {p.description && <span className="hint">{p.description}</span>}
            <span className="spacer" />
            <button className="link" onClick={props.onClose}>
              Close
            </button>
          </>
        }
      >
        {option ? <OptionMetrics o={p} /> : <StockMetrics s={p} />}
      </Card>
      <div className="grid cols-2 top">
        <div className="grid">
          <Card title="What to know" hint="computed from the figures above; not advice">
            <Notes notes={p.notes} />
          </Card>
          {(p.actions?.length ?? 0) > 0 && (
            <Card title="What you could do" hint="priced from the latest option chain; you decide">
              <Actions actions={p.actions!} />
            </Card>
          )}
        </div>
        <AskPanel
          symbol={p.symbol}
          title={`Ask Claude about ${option ? "this contract" : p.symbol}`}
          ask={props.ask}
          thread={props.thread}
          onThread={props.onThread}
          suggestions={
            option
              ? [
                  "Should I hold this to expiry or sell now?",
                  "What has to happen for this to pay off, and how likely is it?",
                  "Is there earnings or news before it expires?",
                ]
              : ["What's the risk in this position?", "Should I trim, hold or add here?"]
          }
        />
      </div>
      {option && p.stats.scenarios && <Scenarios grid={p.stats.scenarios} cost={p.cost} quantity={p.quantity} />}
    </section>
  );
}

function Metric({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  );
}

function OptionMetrics({ o }: { o: OptionPositionRow }) {
  const s = o.stats;
  const quoted = s.quote_at ? ` · quote ${timeAgo(s.quote_at)}` : "";
  return (
    <div className="metrics">
      <Metric label="Value" value={money(s.value ?? o.market_value)} sub={`${qty(o.quantity)} × ${money(s.mark, true)} × 100`} />
      <Metric label="P&L" value={<Delta value={s.pnl} kind="money" />} sub={<>paid {money(s.cost, true)} a share · <Delta value={s.pnl_pct} digits={0} /></>} />
      <Metric label="Bid / ask" value={s.bid != null ? `${money(s.bid, true)} / ${money(s.ask, true)}` : "—"} sub={`mark from ${s.mark_source}${quoted}`} />
      <Metric label="Sell now (at the bid)" value={money(s.exit_value)} sub={s.spread_pct != null ? `spread ${pct(s.spread_pct, 0)} of price` : null} />
      <Metric label={`${o.underlying} price`} value={money(s.spot, true)} sub={s.moneyness == null ? null : `${s.in_the_money ? "in" : "out of"} the money by ${pct(Math.abs(s.moneyness), 1)}`} />
      <Metric label="Breakeven at expiry" value={money(s.breakeven, true)} sub={needs(s.breakeven_move_pct, s.right, o.quantity)} />
      <Metric label="Expires" value={s.expiration} sub={`${Math.max(s.days_left, 0).toFixed(1)} days left`} />
      <Metric label="Time decay" value={s.theta_day != null ? `${signedMoney(s.theta_day)}/day` : "—"} sub={s.extrinsic != null ? `${money(s.extrinsic, true)} of the price is time value` : null} />
      <Metric label="Odds of profit at expiry" value={pct(s.prob_profit, 0)} sub={`in the money: ${pct(s.prob_itm, 0)}`} />
      <Metric label="Market-implied move" value={s.expected_move_pct != null ? `±${pct(s.expected_move_pct, 1)}` : "—"} sub={s.expected_move != null ? `±${money(s.expected_move, true)} by expiry (1σ)` : null} />
      <Metric
        label="Implied volatility"
        value={pct(s.iv, 0)}
        sub={
          s.early_exercise_value != null && s.early_exercise_value >= 0.05
            ? `American: ${money(s.early_exercise_value, true)} a share is the right to exercise early`
            : s.realized_vol != null
              ? `stock's 20-day: ${pct(s.realized_vol, 0)}`
              : null
        }
      />
      <Metric label="Delta" value={num(s.delta, 2)} sub={s.delta_shares != null ? `acts like ${num(s.delta_shares, 0)} shares` : null} />
      <Metric label="Gamma" value={num(s.gamma, 3)} sub="delta change per $1" />
      <Metric label="Vega" value={s.vega_point != null ? signedMoney(s.vega_point) : "—"} sub="per 1-point IV change" />
    </div>
  );
}

function StockMetrics({ s }: { s: StockPosition }) {
  const st = s.stats;
  const r = st.returns;
  return (
    <div className="metrics">
      <Metric label="Value" value={money(s.market_value)} sub={`${qty(s.quantity)} × ${money(s.price, true)} · ${pct(s.weight, 1)} of portfolio`} />
      <Metric label="P&L" value={<Delta value={s.pnl} kind="money" />} sub={<>avg cost {money(s.cost, true)} · <Delta value={s.pnl_pct} digits={1} /></>} />
      <Metric label="Today" value={<Delta value={st.day_change_pct} />} sub={s.day_pnl != null ? signedMoney(s.day_pnl) : `price from ${s.price_source}`} />
      <Metric label="1 week" value={<Delta value={r?.["1W"]} digits={1} />} sub={<>1 month <Delta value={r?.["1M"]} digits={1} /></>} />
      <Metric label="3 months" value={<Delta value={r?.["3M"]} digits={1} />} sub={<>6 months <Delta value={r?.["6M"]} digits={1} /></>} />
      <Metric label="1 year" value={<Delta value={r?.["1Y"]} digits={1} />} sub={<>this year <Delta value={st.ytd} digits={1} /></>} />
      <Metric label="52-week range" value={`${money(st.low_52w, true)} – ${money(st.high_52w, true)}`} sub={st.from_high_pct != null ? `${pct(-st.from_high_pct, 0)} below the high` : null} />
      <Metric label="vs 50 / 200-day avg" value={<><Delta value={st.vs_ma50} digits={1} /> <Delta value={st.vs_ma200} digits={1} /></>} sub={`${money(st.ma50, true)} / ${money(st.ma200, true)}`} />
      <Metric label="Volatility" value={pct(st.vol_20d, 0)} sub={`20-day, a year · 60-day ${pct(st.vol_60d, 0)}`} />
      <Metric label="Beta to S&P 500" value={num(st.beta_1y, 2)} sub="1 year of daily moves" />
      <Metric label="Worst drop, 1 year" value={pct(st.max_drawdown_1y, 0)} sub="peak to trough" />
    </div>
  );
}

export function Actions({ actions }: { actions: HoldingAction[] }) {
  const kind = { attention: "attention", consider: "consider", info: "info" } as const;
  return (
    <div className="notes">
      {actions.map((a) => (
        <div key={a.title} className={`note idea ${kind[a.level]}`}>
          <strong>{a.title}</strong>
          <div>{a.text}</div>
          {a.trade && (
            <div className="small muted">
              {a.trade.label} · bid {money(a.trade.bid, true)} / ask {money(a.trade.ask, true)} · delta {num(a.trade.delta, 2)} · {a.trade.dte} days
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function Notes({ notes }: { notes: PositionNote[] }) {
  if (!notes.length) return <Empty>Nothing stands out.</Empty>;
  const kind = { risk: "attention", warn: "consider", info: "info" } as const;
  return (
    <div className="notes">
      {notes.map((n) => (
        <div key={n.title} className={`note idea ${kind[n.level]}`}>
          <strong>{n.title}</strong> <span className="muted small">{n.level === "risk" ? "· act before expiry" : null}</span>
          <div>{n.text}</div>
        </div>
      ))}
    </div>
  );
}

/** Position P&L across underlying moves and dates, at today's implied volatility. */
export function Scenarios({ grid, cost, quantity }: { grid: ScenarioGrid; cost: number | null; quantity: number }) {
  return (
    <Card title="Scenarios" hint={`position P&L if the underlying moves, ${cost != null ? `against ${money(cost * 100 * Math.abs(quantity))} paid` : ""} · model estimate at today's implied volatility`}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Underlying</th>
              {grid.columns.map((c) => (
                <th key={c} className="num">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.map((r) => (
              <tr key={r.move} className={r.move === 0 ? "selected" : undefined}>
                <td>
                  {r.move === 0 ? "unchanged" : pct(r.move, 0, true)} <span className="muted small">{money(r.price, true)}</span>
                </td>
                {r.cells.map((c, i) => (
                  <td key={grid.columns[i]} className="num">
                    <Delta value={c.pnl} kind="money" />
                    <div className="small muted">worth {money(c.value)}</div>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// -- asking Claude ------------------------------------------------------------------------------

export function AskPanel(props: {
  endpoint?: string; // /api/positions/ask (a holding) or /api/research/ask (an idea)
  symbol: string | null;
  title: string;
  ask: PositionsData["ask"];
  thread: ChatTurn[];
  onThread: (t: ChatTurn[]) => void;
  suggestions: string[];
}) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<AskReply | null>(null);

  if (!props.ask?.available) {
    return (
      <Card title={props.title}>
        <Empty>
          Put an Anthropic API key in <code>.env</code> as <code>ANTHROPIC_API_KEY=…</code> and restart <code>tp-api</code> to ask Claude. Each question
          sends the figures on this page and a summary of your portfolio to Anthropic's API, and is billed to your account.
        </Empty>
      </Card>
    );
  }

  async function send(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    try {
      const reply = await postJson<AskReply>(props.endpoint ?? "/api/positions/ask", {
        question: q,
        symbol: props.symbol,
        history: props.thread,
      });
      props.onThread([...props.thread, { role: "user", content: q }, { role: "assistant", content: reply.answer }]);
      setMeta(reply);
      setQuestion("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={props.title} hint={`${props.ask.model} · sees live figures for your positions`}>
      <div className="chat" aria-live="polite">
        {props.thread.map((t, i) => (
          <div key={i} className={`turn ${t.role}`}>
            <div className="who small muted">{t.role === "user" ? "You" : "Claude"}</div>
            <div className="text">{t.content}</div>
          </div>
        ))}
        {busy && <div className="muted small">Claude is thinking (can take up to a minute with a web search)…</div>}
      </div>
      {meta && meta.sources.length > 0 && (
        <div className="small muted sources">
          Sources:{" "}
          {meta.sources.slice(0, 5).map((s, i) => (
            <span key={s.url}>
              {i > 0 && " · "}
              <a href={s.url} target="_blank" rel="noreferrer noopener">
                {s.title}
              </a>
            </span>
          ))}
        </div>
      )}
      {meta?.note && <div className="small muted">{meta.note}</div>}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {props.thread.length === 0 && (
        <div className="toolbar">
          {props.suggestions.map((s) => (
            <button key={s} className="chip" onClick={() => send(s)} disabled={busy}>
              {s}
            </button>
          ))}
        </div>
      )}
      <form
        className="ask"
        onSubmit={(e) => {
          e.preventDefault();
          void send(question);
        }}
      >
        <textarea
          aria-label="Your question"
          rows={2}
          value={question}
          placeholder={props.thread.length ? "Follow up…" : "Ask anything about it"}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send(question);
            }
          }}
        />
        <div className="toolbar">
          <button type="submit" className="primary" disabled={busy || !question.trim()}>
            {busy ? "Asking…" : "Ask"}
          </button>
          {props.thread.length > 0 && (
            <button type="button" className="link" onClick={() => props.onThread([])} disabled={busy}>
              New conversation
            </button>
          )}
          {meta && (
            <span className="muted small">
              {meta.usage.input_tokens.toLocaleString()} in / {meta.usage.output_tokens.toLocaleString()} out tokens
              {meta.searches ? ` · ${meta.searches} web search${meta.searches === 1 ? "" : "es"}` : ""}
            </span>
          )}
        </div>
      </form>
    </Card>
  );
}
