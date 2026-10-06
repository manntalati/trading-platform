import { useState } from "react";
import { postJson, useApi } from "../api";
import { GrowthChart, useThemeColors } from "../components/charts";
import { Card, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { money, num, nyTime, pct, qty, timeAgo } from "../format";
import type { GrowthPoint, PaperBot, PaperEvent, PaperHistory, PaperProposal, PaperSleeve, PaperStatus } from "../types";

/** Paper trading: the bot's queued trades (with a veto), anything waiting for approval, and
 * each sleeve's progress toward the paper gate. */
export default function Paper(props: { onChange?: () => void }) {
  const status = useApi<PaperStatus>("/api/paper", 30_000);
  const pending = useApi<PaperProposal[]>("/api/paper/proposals", 30_000);
  const queued = useApi<PaperProposal[]>("/api/paper/proposals?scope=queued", 30_000);
  const recent = useApi<PaperProposal[]>("/api/paper/proposals?scope=recent&limit=15", 60_000);
  const history = useApi<PaperHistory>("/api/paper/history", 300_000);
  const events = useApi<PaperEvent[]>("/api/paper/events?limit=12", 60_000);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = () => {
    status.reload();
    pending.reload();
    queued.reload();
    recent.reload();
    events.reload();
    props.onChange?.();
  };

  async function act(key: string, path: string, body: unknown) {
    setBusy(key);
    setActionError(null);
    try {
      await postJson(path, body);
      refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const s = status.data;
  if (status.loading && !s) return <Loading />;
  return (
    <div className="grid">
      <ErrorBanner error={status.error} />
      <ErrorBanner error={actionError} />
      {s?.broker_error && (
        <div className="banner">
          Paper account unavailable: {s.broker_error}. Proposals can still be decided; submitting them needs the broker.
        </div>
      )}
      {s && <Tiles status={s} />}

      <Card title="Queued for the next open" hint="the bot sends these as market-on-open orders at 9:10am ET">
        <ErrorBanner error={queued.error} />
        {queued.data && queued.data.length > 0 ? (
          <ul className="proposals">
            {queued.data.map((p) => (
              <QueuedRow
                key={p.id}
                p={p}
                busy={busy !== null}
                onVeto={() => act(p.id, `/api/paper/proposals/${encodeURIComponent(p.id)}/reject`, { note: "vetoed before submission" })}
              />
            ))}
          </ul>
        ) : (
          <Empty>Nothing queued. The bot runs the strategies after each close and queues what passes the risk checks.</Empty>
        )}
      </Card>

      {pending.data && pending.data.length > 0 && (
        <Card title="Waiting for your decision" hint="strategies on manual approval; undecided proposals expire at 9:10am ET">
          <ErrorBanner error={pending.error} />
          <Proposals
            proposals={pending.data}
            busy={busy}
            onApprove={(p, shares) => act(p.id, `/api/paper/proposals/${encodeURIComponent(p.id)}/approve`, { quantity: shares })}
            onReject={(p) => act(p.id, `/api/paper/proposals/${encodeURIComponent(p.id)}/reject`, { note: "" })}
            onApproveAll={(strategy) => act(`all:${strategy}`, "/api/paper/approve-all", { strategy })}
          />
        </Card>
      )}

      {s && <Sleeves sleeves={s.sleeves} gate={s.gate} />}
      {s && <EquityChart history={history.data} sleeves={s.sleeves} />}

      <div className="grid cols-2 top">
        <Card title="Recent orders" hint="latest 15">
          {recent.data && recent.data.length > 0 ? <RecentOrders rows={recent.data} /> : <Empty>No orders yet.</Empty>}
        </Card>
        <div className="grid">
          {s && <KillSwitch status={s} busy={busy === "kill"} onKill={(reason) => act("kill", "/api/paper/kill", { reason })} />}
          <Card title="Activity">
            {events.data && events.data.length > 0 ? (
              <ul className="events">
                {events.data.map((e) => (
                  <li key={e.id}>
                    <span className="muted small">{timeAgo(e.at)}</span> <strong>{e.kind}</strong> {e.message}
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>No activity yet.</Empty>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Tiles(props: { status: PaperStatus }) {
  const { account, kill_switch: kill, reconciliation: rec } = props.status;
  const breaks = rec ? Object.keys(rec.breaks).length : 0;
  return (
    <div className="grid tiles">
      <Stat
        label="Paper account"
        value={account ? money(account.equity) : "—"}
        sub={account ? `${props.status.broker} ${account.number} · cash ${money(account.cash)}` : props.status.broker}
      />
      <BotTile bot={props.status.bot} />
      <Stat
        label="Kill switch"
        value={
          <span>
            <span className={`dot ${kill ? "critical" : "good"}`} aria-hidden="true" /> {kill ? "Engaged" : "Off"}
          </span>
        }
        sub={kill ? kill.reason : "orders can be submitted"}
      />
      <Stat
        label="Reconciliation"
        value={
          <span>
            <span className={`dot ${!rec ? "" : rec.ok ? "good" : "critical"}`} aria-hidden="true" />{" "}
            {!rec ? "Not run" : rec.ok ? "Matches broker" : `${breaks} break${breaks === 1 ? "" : "s"}`}
          </span>
        }
        sub={rec ? `checked ${timeAgo(rec.at)}` : "runs with every sync"}
      />
    </div>
  );
}

const TASKS: Record<string, string> = {
  submit: "send orders",
  sync_open: "record fills",
  sync_close: "after-close sync",
  propose: "run strategies",
};

function BotTile(props: { bot: PaperBot | null }) {
  const bot = props.bot;
  if (!bot) {
    return <Stat label="Bot" value="Not started" sub={<>start it with <code>uv run tp-paper bot</code></>} />;
  }
  const next = bot.next ? `next: ${TASKS[bot.next.task] ?? bot.next.task}, ${nyTime(bot.next.due)} ET` : "nothing scheduled";
  return (
    <Stat
      label="Bot"
      value={
        <span>
          <span className={`dot ${bot.alive ? "good pulse" : "critical"}`} aria-hidden="true" /> {bot.alive ? "Running" : "Stopped"}
        </span>
      }
      sub={bot.alive ? next : `last seen ${timeAgo(bot.heartbeat)}`}
    />
  );
}

export function QueuedRow(props: { p: PaperProposal; busy: boolean; onVeto: () => void }) {
  const { p } = props;
  const [open, setOpen] = useState(false);
  return (
    <li className="proposal">
      <div className="proposal-head">
        <span className="side">{p.side === "buy" ? "▲ Buy" : "▼ Sell"}</span>
        <strong>
          {qty(p.order_quantity)} {p.symbol}
        </strong>
        <span className="muted">
          ≈ {money(p.notional)} · {p.strategy}
        </span>
        <span className="spacer" />
        <button disabled={props.busy} onClick={props.onVeto}>
          Don't trade
        </button>
      </div>
      <p className="reason">{p.reason}</p>
      <button className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
        {p.checks.length} risk checks passed {open ? "▴" : "▾"}
      </button>
      {open && (
        <ul className="checks">
          {p.checks.map((c) => (
            <li key={c.check}>
              <span aria-label={c.passed ? "passed" : "failed"}>{c.passed ? "✓" : "✗"}</span> <code>{c.check}</code> {c.detail}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Proposals(props: {
  proposals: PaperProposal[];
  busy: string | null;
  onApprove: (p: PaperProposal, shares: number | null) => void;
  onReject: (p: PaperProposal) => void;
  onApproveAll: (strategy: string) => void;
}) {
  const groups = new Map<string, PaperProposal[]>();
  for (const p of props.proposals) groups.set(p.strategy, [...(groups.get(p.strategy) ?? []), p]);
  return (
    <div className="proposal-groups">
      {[...groups.entries()].map(([strategy, rows]) => (
        <div key={strategy} className="proposal-group">
          <div className="toolbar">
            <strong>{strategy}</strong>
            <span className="muted small">
              {rows.length} order{rows.length === 1 ? "" : "s"} from the {rows[0]?.session} close
            </span>
            <span className="spacer" />
            <button disabled={props.busy !== null} onClick={() => props.onApproveAll(strategy)}>
              Approve all {rows.length}
            </button>
          </div>
          <ul className="proposals">
            {rows.map((p) => (
              <ProposalRow key={p.id} p={p} busy={props.busy !== null} onApprove={props.onApprove} onReject={props.onReject} />
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function ProposalRow(props: {
  p: PaperProposal;
  busy: boolean;
  onApprove: (p: PaperProposal, shares: number | null) => void;
  onReject: (p: PaperProposal) => void;
}) {
  const { p } = props;
  const [shares, setShares] = useState(p.order_quantity);
  const [open, setOpen] = useState(false);
  const failed = p.checks.filter((c) => !c.passed).length;
  const valid = Number.isInteger(shares) && shares >= 1 && shares <= p.quantity;
  return (
    <li className="proposal">
      <div className="proposal-head">
        <span className="side">{p.side === "buy" ? "▲ Buy" : "▼ Sell"}</span>
        <strong>
          {qty(p.order_quantity)} {p.symbol}
        </strong>
        <span className="muted">
          ≈ {money(p.notional)} at {money(p.reference_price, true)}
        </span>
      </div>
      <p className="reason">{p.reason}</p>
      <button className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
        {failed ? `${failed} risk check(s) failed` : `${p.checks.length} risk checks passed`} {open ? "▴" : "▾"}
      </button>
      {open && (
        <ul className="checks">
          {p.checks.map((c) => (
            <li key={c.check}>
              <span aria-label={c.passed ? "passed" : "failed"}>{c.passed ? "✓" : "✗"}</span> <code>{c.check}</code> {c.detail}
            </li>
          ))}
        </ul>
      )}
      <div className="actions">
        <label>
          Shares{" "}
          <input
            type="number"
            min={1}
            max={p.quantity}
            step={1}
            value={shares}
            aria-label={`Shares of ${p.symbol} to ${p.side}`}
            onChange={(e) => setShares(Number(e.target.value))}
          />
        </label>
        <button className="primary" disabled={props.busy || !valid} onClick={() => props.onApprove(p, shares === p.quantity ? null : shares)}>
          Approve
        </button>
        <button disabled={props.busy} onClick={() => props.onReject(p)}>
          Reject
        </button>
        {!valid && <span className="small muted">1 to {qty(p.quantity)} whole shares</span>}
      </div>
    </li>
  );
}

function Progress(props: { value: number; max: number; label: string }) {
  const share = Math.min(1, props.max ? props.value / props.max : 0);
  return (
    <div className="progress" title={props.label}>
      <div className="bar-track" role="progressbar" aria-valuenow={props.value} aria-valuemax={props.max} aria-label={props.label}>
        <div className="bar-fill" style={{ width: `${share * 100}%` }} />
      </div>
      <span className="small">
        {props.value}/{props.max}
      </span>
    </div>
  );
}

function Sleeves(props: { sleeves: PaperSleeve[]; gate: { days: number; trades: number } }) {
  return (
    <Card title="Sleeves" hint={`paper gate: ${props.gate.days} trading days and ${props.gate.trades} trades, results in line with the backtest`}>
      {props.sleeves.length === 0 ? (
        <Empty>
          No strategies in the paper book (<code>config/paper.toml</code>).
        </Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th className="num">Equity</th>
                <th className="num">Return</th>
                <th className="num">Max drawdown</th>
                <th className="num">Sharpe</th>
                <th>Trading days</th>
                <th>Trades</th>
                <th className="num">Slippage</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {props.sleeves.map((s) => (
                <tr key={s.strategy}>
                  <td>
                    <strong>{s.strategy}</strong>
                    <div className="small muted">
                      {money(s.capital)} · {s.approval} approval
                    </div>
                  </td>
                  <td className="num">{money(s.equity)}</td>
                  <td className="num">{pct(s.return, 2, true)}</td>
                  <td className="num">{pct(s.max_drawdown, 1)}</td>
                  <td className="num">{s.sharpe == null ? "—" : num(s.sharpe, 2)}</td>
                  <td>
                    <Progress value={s.gate.days[0]} max={s.gate.days[1]} label={`${s.strategy} trading days`} />
                  </td>
                  <td>
                    <Progress value={s.gate.trades[0]} max={s.gate.trades[1]} label={`${s.strategy} trades`} />
                  </td>
                  <td className="num" title={`backtest assumes ${s.modeled_slippage_bps} bps`}>
                    {s.slippage_bps == null ? "—" : `${num(s.slippage_bps, 1)} bps`}
                    <div className="small muted">model {s.modeled_slippage_bps}</div>
                  </td>
                  <td>
                    {s.disabled ? (
                      <span>
                        <span className="dot critical" aria-hidden="true" /> Disabled: {s.disabled}
                      </span>
                    ) : (
                      <span>
                        <span className="dot good" aria-hidden="true" /> Active
                        {s.pending ? <span className="muted"> · {s.pending} pending</span> : null}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function EquityChart(props: { history: PaperHistory | null; sleeves: PaperSleeve[] }) {
  const theme = useThemeColors();
  const names = props.sleeves.map((s) => s.strategy);
  const capital = new Map(props.sleeves.map((s) => [s.strategy, s.capital]));
  const bySession = new Map<string, GrowthPoint>();
  for (const name of names) {
    for (const day of props.history?.[name] ?? []) {
      const base = capital.get(name);
      if (!base) continue;
      const row: GrowthPoint = bySession.get(day.session) ?? { date: day.session };
      row[name] = day.equity / base;
      bySession.set(day.session, row);
    }
  }
  const data = [...bySession.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  return (
    <Card title="Sleeve equity" hint="growth of $1 of each sleeve's capital, marked at every close">
      {data.length >= 2 ? (
        <GrowthChart data={data} series={names} colors={names.map((_, i) => theme.series[i] ?? theme.reference)} />
      ) : (
        <Empty>The curve starts after the first evenings of paper trading.</Empty>
      )}
    </Card>
  );
}

const DONE = new Set(["filled"]);

function RecentOrders(props: { rows: PaperProposal[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Session</th>
            <th>Order</th>
            <th>Status</th>
            <th className="num">Fill</th>
          </tr>
        </thead>
        <tbody>
          {props.rows.map((p) => (
            <tr key={p.id} title={p.reason}>
              <td>
                {p.session}
                <div className="small muted">{p.strategy}</div>
              </td>
              <td>
                {p.side === "buy" ? "Buy" : "Sell"} {qty(p.order_quantity)} {p.symbol}
              </td>
              <td>
                <span className={DONE.has(p.status) ? "" : "muted"}>{p.status.replace("_", " ")}</span>
                {p.note && <div className="small muted">{p.note}</div>}
              </td>
              <td className="num">{p.avg_fill_price == null ? "—" : money(p.avg_fill_price, true)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KillSwitch(props: { status: PaperStatus; busy: boolean; onKill: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  const kill = props.status.kill_switch;
  return (
    <Card title="Kill switch" hint="cancels open orders and blocks new ones">
      {kill ? (
        <p>
          <span className="dot critical" aria-hidden="true" /> Engaged {timeAgo(kill.at)}: {kill.reason}. Lift it from the
          terminal with <code>uv run tp-risk resume</code>.
        </p>
      ) : (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            if (reason.trim().length >= 3) props.onKill(reason.trim());
          }}
        >
          <input
            aria-label="Reason for stopping"
            placeholder="Why? (e.g. bad data)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button className="danger" type="submit" disabled={props.busy || reason.trim().length < 3}>
            Stop all trading
          </button>
        </form>
      )}
    </Card>
  );
}
