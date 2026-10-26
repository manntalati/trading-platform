import { useState, type ReactNode } from "react";
import { useApi } from "../api";
import { money, nyTime, pct, signedMoney, timeAgo } from "../format";
import type {
  JobsStatus,
  MarketRegime,
  PaperStatus,
  Portfolio,
  Positions,
  Research,
  Status,
  TradeStats,
} from "../types";

/** The side column: panels that summarise one area each and open it in the middle. Which ones
 * are expanded is remembered on this browser. */

const KEY = "tp-rail";

function loadOpen(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

export function useRailOpen(): [Record<string, boolean>, (id: string, open: boolean) => void] {
  const [open, setOpen] = useState<Record<string, boolean>>(loadOpen);
  const set = (id: string, value: boolean) => {
    const next = { ...open, [id]: value };
    setOpen(next);
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* private mode: the layout just won't be remembered */
    }
  };
  return [open, set];
}

/** A 0..1 bar, like a usage meter: ``tone`` colours it. */
export function Meter(props: { value: number | null | undefined; tone?: "accent" | "good" | "warning" | "critical"; label: string }) {
  const v = props.value == null || !Number.isFinite(props.value) ? 0 : Math.max(0, Math.min(1, props.value));
  return (
    <span className={`meter ${props.tone ?? "accent"}`} role="meter" aria-label={props.label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)}>
      <span style={{ width: `${v * 100}%` }} />
    </span>
  );
}

export function RailPanel(props: {
  id: string;
  title: string;
  open: boolean;
  onToggle: (open: boolean) => void;
  summary?: ReactNode;
  href?: string;
  /** The panel's main request: until it answers, the panel says so rather than "nothing here". */
  source?: { data: unknown; loading: boolean; error: string | null };
  children?: ReactNode;
}) {
  const body = `rail-${props.id}`;
  const src = props.source;
  const waiting = src != null && src.data == null && (src.loading || src.error != null);
  const summary = waiting ? <span className="muted">{src.loading ? "…" : "unavailable"}</span> : props.summary;
  const children = waiting ? <RailRow label={src.loading ? "Loading…" : `Couldn't load: ${src.error}`} /> : props.children;
  return (
    <section className={`rail-panel ${props.open ? "open" : ""}`} aria-labelledby={`${body}-title`}>
      <header>
        <button
          className="rail-toggle"
          aria-expanded={props.open}
          aria-controls={body}
          onClick={() => props.onToggle(!props.open)}
        >
          <span id={`${body}-title`}>{props.title}</span>
          <span className="chevron" aria-hidden="true">⌄</span>
        </button>
        <span className="rail-summary">{summary}</span>
        {props.href && (
          <a className="rail-open" href={props.href} aria-label={`Open ${props.title}`} title={`Open ${props.title}`}>
            ›
          </a>
        )}
      </header>
      {props.open && (
        <div className="rail-body" id={body}>
          {children}
        </div>
      )}
    </section>
  );
}

export function RailRow(props: { label: ReactNode; value?: ReactNode; href?: string; icon?: ReactNode; mono?: boolean }) {
  const content = (
    <>
      {props.icon && <span className="rail-icon" aria-hidden="true">{props.icon}</span>}
      <span className="rail-label">{props.label}</span>
      {props.value != null && <span className={`rail-value ${props.mono === false ? "" : "mono"}`}>{props.value}</span>}
    </>
  );
  return props.href ? (
    <a className="rail-row link" href={props.href}>
      {content}
    </a>
  ) : (
    <div className="rail-row">{content}</div>
  );
}

function Dot(props: { tone: "good" | "warning" | "critical" | "muted" }) {
  return <span className={`dot ${props.tone === "muted" ? "" : props.tone}`} aria-hidden="true" />;
}

function signedPct(v: number | null | undefined, digits = 0) {
  return pct(v, digits, true);
}

function days(v: number | null | undefined) {
  if (v == null || !Number.isFinite(v)) return "—";
  return v < 1 ? "same day" : `${Math.round(v)}d`;
}

// -- the panels ---------------------------------------------------------------------------------

type PanelProps = { open: boolean; onToggle: (open: boolean) => void };

export function AccountPanel(p: PanelProps) {
  const portfolio = useApi<Portfolio>("/api/portfolio", 120_000);
  const positions = useApi<Positions>(p.open ? "/api/positions" : null, 60_000);
  const d = portfolio.data;
  const totals = positions.data?.totals;
  const problems = d?.freshness?.problems ?? [];
  return (
    <RailPanel
      id="account"
      title="Account"
      href="#/portfolio"
      {...p}
      source={portfolio}
      summary={d?.synced ? <span className="mono">{money(d.total_value)}</span> : <span className="muted">not synced</span>}
    >
      {!d?.synced && <RailRow label="Link Fidelity" value="tp-broker link" />}
      {d?.synced && (
        <>
          {totals && <RailRow label="Today" value={<span className={totals.day_pnl >= 0 ? "up" : "down"}>{signedMoney(totals.day_pnl)}</span>} />}
          {totals && <RailRow label="Unrealized" value={<span className={totals.pnl >= 0 ? "up" : "down"}>{signedMoney(totals.pnl)}</span>} />}
          <RailRow label="Cash" value={money(d.cash)} />
          {d.accounts.map((a) => (
            <RailRow key={`${a.source}${a.account_number_masked}`} icon="▣" label={`${a.institution ?? a.source} ${a.account_number_masked ?? ""}`} value={money(a.total_value)} href="#/portfolio" />
          ))}
          <RailRow
            icon={<Dot tone={problems.length ? "critical" : "good"} />}
            label={problems.length ? "Fidelity data not updating" : "Synced"}
            value={timeAgo(d.freshness?.synced_at)}
            href="#/trades/all"
          />
        </>
      )}
    </RailPanel>
  );
}

export function MarketPanel(p: PanelProps) {
  const regime = useApi<MarketRegime>("/api/market/regime", 600_000);
  const status = useApi<Status>(p.open ? "/api/status" : null, 60_000);
  const c = regime.data?.current;
  const clock = status.data?.market;
  const vixTone = c?.volatility === "stressed" ? "critical" : c?.volatility === "calm" ? "good" : "warning";
  return (
    <RailPanel
      id="market"
      title="Market"
      href="#/research/lookup/SPY"
      {...p}
      source={regime}
      summary={c ? <span className="muted">{[c.trend && `${c.trend}trend`, c.volatility].filter(Boolean).join(" · ")}</span> : null}
    >
      {clock && (
        <RailRow
          icon={<Dot tone={clock.is_open ? "good" : "muted"} />}
          label={clock.is_open ? "Market open" : "Market closed"}
          value={clock.is_open ? `closes ${nyTime(clock.next_close)}` : `opens ${nyTime(clock.next_open)}`}
          mono={false}
        />
      )}
      {c && (
        <>
          <RailRow label="SPY vs 200-day" value={signedPct(c.spy_vs_ma200, 1)} href="#/research/lookup/SPY" />
          {c.vix != null && (
            <RailRow
              label="VIX"
              value={
                <>
                  {c.vix.toFixed(1)} <Meter value={c.vix / 50} tone={vixTone} label="VIX against 50" />
                </>
              }
            />
          )}
          {c.breadth && <RailRow label="Breadth" value={`${c.breadth} · ${pct(c.breadth_pct, 0)}`} />}
          {c.curve && <RailRow label="Yield curve" value={`${c.curve}${c.curve_10y_3m != null ? ` · ${c.curve_10y_3m.toFixed(2)} pts` : ""}`} />}
          {c.rates && <RailRow label="Rates" value={c.rates} />}
        </>
      )}
      {!c && <RailRow label="No market data yet" value="tp-data bars backfill" />}
    </RailPanel>
  );
}

export function TradingPanel(p: PanelProps) {
  const stats = useApi<TradeStats>("/api/trades/stats", 300_000);
  const s = stats.data;
  const all = s?.by_kind?.all;
  const has = (all?.trades ?? 0) > 0;
  return (
    <RailPanel
      id="trading"
      title="Your trading"
      href="#/trades/stats"
      {...p}
      source={stats}
      summary={
        has ? (
          <>
            <span className="mono">{pct(all?.win_rate, 0)}</span> <Meter value={all?.win_rate} tone={(all?.win_rate ?? 0) >= 0.5 ? "good" : "warning"} label="Win rate" />
          </>
        ) : null
      }
    >
      {!has && <RailRow label="No closed trades in the synced history yet" />}
      {has && s?.by_kind && (
        <>
          <RailRow label="Typical return" value={signedPct(all?.median_return, 0)} href="#/trades/stats" />
          <RailRow label="Realized" value={<span className={(all?.total_pnl ?? 0) >= 0 ? "up" : "down"}>{signedMoney(all?.total_pnl)}</span>} />
          {(["calls", "puts", "shares", "options_sold"] as const).map((k) => {
            const g = s.by_kind![k];
            if (!g || !g.trades) return null;
            const name = { calls: "Calls", puts: "Puts", shares: "Shares", options_sold: "Options sold" }[k];
            return (
              <RailRow
                key={k}
                icon={k === "shares" ? "◼" : "◇"}
                label={`${name} · ${g.trades}`}
                value={`${signedPct(g.median_return)} · held ${days(g.median_days)}`}
                href="#/trades/stats"
              />
            );
          })}
        </>
      )}
    </RailPanel>
  );
}

const IDEA_LISTS = [
  { key: "swing", label: "Swing" },
  { key: "leaps", label: "LEAPS" },
  { key: "bearish", label: "Bearish" },
  { key: "income", label: "Premium" },
  { key: "volatility", label: "Volatility" },
] as const;

/** The latest recommendations: the top idea of each list, what's new since the previous session. */
export function IdeasPanel(p: PanelProps) {
  const research = useApi<Research>("/api/research", 120_000);
  const r = research.data;
  const fresh = r ? IDEA_LISTS.reduce((n, l) => n + (r[l.key] ?? []).filter((i) => i.new).length, 0) : 0;
  return (
    <RailPanel
      id="ideas"
      title="Ideas"
      href="#/research"
      {...p}
      source={research}
      summary={r ? <span className={fresh ? "" : "muted"}>{fresh ? `${fresh} new` : r.run ? nyTime(r.run.as_of) : ""}</span> : null}
    >
      {r &&
        IDEA_LISTS.map(({ key, label }) => {
          const list = r[key] ?? []; // runs stored before a list existed lack it
          const top = list[0];
          if (!top) return <RailRow key={key} label={`${label}: nothing right now`} />;
          return (
            <RailRow
              key={key}
              icon={top.new ? <Dot tone="good" /> : "▸"}
              label={`${label} · ${top.symbol}`}
              value={`${list.length} listed`}
              href={`#/research/lookup/${encodeURIComponent(top.symbol)}`}
            />
          );
        })}
      {r?.run && <RailRow label="Updated" value={nyTime(r.run.as_of)} mono={false} />}
      <RailRow label="How past ideas did" value="›" href="#/research/track-record" />
    </RailPanel>
  );
}

export function PaperPanel(p: PanelProps) {
  const paper = useApi<PaperStatus>("/api/paper", 60_000);
  const d = paper.data;
  const alive = d?.bot?.alive ?? false;
  return (
    <RailPanel
      id="paper"
      title="Paper bot"
      href="#/paper"
      {...p}
      source={paper}
      summary={
        d ? (
          <>
            <Dot tone={d.kill_switch ? "critical" : alive ? "good" : "warning"} /> <span className="muted">{d.kill_switch ? "stopped" : alive ? "running" : "idle"}</span>
          </>
        ) : null
      }
    >
      {d && (
        <>
          {d.pending > 0 && <RailRow icon={<Dot tone="warning" />} label={`${d.pending} waiting for approval`} href="#/paper" />}
          {d.account && <RailRow label="Paper equity" value={money(d.account.equity)} />}
          {d.sleeves.map((s) => (
            <RailRow key={s.strategy} icon="▸" label={s.strategy} value={signedPct(s.return, 1)} href="#/paper/strategies" />
          ))}
          {d.bot?.next && <RailRow label={`Next: ${d.bot.next.task}`} value={nyTime(d.bot.next.due)} mono={false} />}
        </>
      )}
    </RailPanel>
  );
}

export function PositionsPanel(p: PanelProps) {
  const positions = useApi<Positions>(p.open ? "/api/positions" : null, 60_000);
  const rows = [...(positions.data?.positions ?? [])].sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, 6);
  return (
    <RailPanel id="positions" title="Positions" href="#/portfolio/positions" {...p} source={positions} summary={rows.length ? <span className="muted">{rows.length}</span> : null}>
      {shown.map((r) => (
        <RailRow
          key={r.symbol}
          icon={r.kind === "option" ? "◇" : "◼"}
          label={r.label}
          value={<span className={(r.pnl_pct ?? 0) >= 0 ? "up" : "down"}>{signedPct(r.pnl_pct, 0)}</span>}
          href={`#/portfolio/positions/${encodeURIComponent(r.symbol)}`}
        />
      ))}
      {rows.length > 6 && (
        <button className="rail-more" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show ${rows.length - 6} more`}
        </button>
      )}
      {!rows.length && !positions.loading && <RailRow label="Nothing held (or not synced)" />}
    </RailPanel>
  );
}

/** What a job's last report says, the same verdict the System page gives: ``null`` when the job
 * hasn't run yet. */
function verdict(report: { ok?: boolean; errors?: unknown } | null | undefined, okDetail: string) {
  if (!report) return { ok: null, detail: "none yet" };
  if (report.ok) return { ok: true, detail: okDetail };
  const failed = report.errors && typeof report.errors === "object" ? Object.keys(report.errors) : [];
  return { ok: false, detail: failed.length === 0 ? "failed" : failed.length <= 2 ? `failed: ${failed.join(", ")}` : `${failed.length} failed` };
}

export function SystemPanel(p: PanelProps) {
  const status = useApi<Status>("/api/status", 60_000);
  const jobs = useApi<JobsStatus>(p.open ? "/api/jobs" : null, 60_000);
  const d = status.data;
  const checks: { label: string; ok: boolean | null; detail: string }[] = d
    ? [
        { label: "Daily bars", ok: d.bars.latest_session != null && !d.bars.stale, detail: d.bars.latest_session ?? "none" },
        { label: "Option chains", ...verdict(d.options.latest_report, String(d.options.latest_report?.snapshot_date ?? "")) },
        { label: "Macro", ...verdict(d.macro?.latest_report, "ok") },
        { label: "Fundamentals", ...verdict(d.fundamentals, `${Object.keys(d.fundamentals?.facts ?? {}).length} companies`) },
        { label: "Fidelity", ok: d.broker.length > 0, detail: d.broker[0] ? timeAgo(d.broker[0].synced_at) : "not linked" },
        { label: "Live prices", ok: !d.live.error, detail: d.live.source },
      ]
    : [];
  const good = checks.filter((c) => c.ok).length;
  const bad = checks.filter((c) => c.ok === false).length;
  return (
    <RailPanel
      id="system"
      title="System"
      href="#/system"
      {...p}
      source={status}
      summary={
        d ? (
          <>
            <span className="mono">
              {good}/{checks.length}
            </span>{" "}
            <Meter value={checks.length ? good / checks.length : 0} tone={bad > 1 ? "critical" : good === checks.length ? "good" : "warning"} label="Data feeds healthy" />
          </>
        ) : null
      }
    >
      {checks.map((c) => (
        <RailRow key={c.label} icon={<Dot tone={c.ok ? "good" : c.ok === false ? "critical" : "muted"} />} label={c.label} value={c.detail} href="#/system" />
      ))}
      {jobs.data && (
        <RailRow
          icon={<Dot tone={jobs.data.running ? "good" : "warning"} />}
          label="Job scheduler"
          value={jobs.data.running ? "running" : "not running"}
          href="#/system"
          mono={false}
        />
      )}
    </RailPanel>
  );
}

const PANELS = [
  { id: "account", Panel: AccountPanel, open: true },
  { id: "trading", Panel: TradingPanel, open: true },
  { id: "ideas", Panel: IdeasPanel, open: true },
  { id: "market", Panel: MarketPanel, open: false },
  { id: "positions", Panel: PositionsPanel, open: false },
  { id: "paper", Panel: PaperPanel, open: false },
  { id: "system", Panel: SystemPanel, open: false },
] as const;

export function Rail() {
  const [open, setOpen] = useRailOpen();
  return (
    <aside className="rail" aria-label="Summary panels">
      {PANELS.map(({ id, Panel, open: initial }) => (
        <Panel key={id} open={open[id] ?? initial} onToggle={(v) => setOpen(id, v)} />
      ))}
    </aside>
  );
}
