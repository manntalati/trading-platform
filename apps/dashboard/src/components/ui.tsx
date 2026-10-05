import type { ReactNode } from "react";
import { pct, signedMoney } from "../format";

export function Card(props: { title?: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${props.className ?? ""}`}>
      {props.title && (
        <h2>
          {props.title}
          {props.hint && <span className="hint">{props.hint}</span>}
        </h2>
      )}
      {props.children}
    </section>
  );
}

export function Stat(props: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="label">{props.label}</div>
      <div className="value">{props.value}</div>
      {props.sub && <div className="sub">{props.sub}</div>}
    </div>
  );
}

/** A change that never relies on color alone: sign, arrow and color together. */
export function Delta(props: { value: number | null | undefined; kind?: "pct" | "money"; digits?: number }) {
  const v = props.value;
  if (v == null || !Number.isFinite(v)) return <span className="delta flat">—</span>;
  const dir = v > 0 ? "up" : v < 0 ? "down" : "flat";
  const arrow = v > 0 ? "▲" : v < 0 ? "▼" : "•";
  const text = props.kind === "money" ? signedMoney(v) : pct(v, props.digits ?? 2, true);
  return (
    <span className={`delta ${dir}`}>
      <span aria-hidden="true">{arrow}</span> {text}
    </span>
  );
}

export function Empty(props: { children: ReactNode }) {
  return <div className="empty">{props.children}</div>;
}

export function ErrorBanner(props: { error: string | null }) {
  if (!props.error) return null;
  return (
    <div className="banner error" role="alert">
      Couldn't load data: {props.error.slice(0, 300)}
    </div>
  );
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}

/** Horizontal bars for part-to-whole by category (sorted, labelled, no legend needed). */
export function BarList(props: { data: Record<string, number>; cap?: number; capLabel?: string }) {
  const rows = Object.entries(props.data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...rows.map(([, v]) => v), props.cap ?? 0, 0.0001);
  return (
    <div className="bars">
      {rows.map(([name, value]) => {
        const over = props.cap !== undefined && value > props.cap;
        return (
          <div className="bar-row" key={name} title={over ? props.capLabel : undefined}>
            <span className="name">{name}</span>
            <div className="bar-track" aria-hidden="true">
              <div className={`bar-fill ${over ? "over" : ""}`} style={{ width: `${(value / max) * 100}%` }} />
            </div>
            <span className="num small">
              {pct(value, 0)}
              {over && <span aria-label="over cap"> ⚠</span>}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export interface StatRow {
  key: string;
  label: string;
  format: (v: number | null) => string;
}

export const STAT_ROWS: StatRow[] = [
  { key: "total_return", label: "Total return", format: (v) => pct(v, 1) },
  { key: "cagr", label: "CAGR", format: (v) => pct(v, 1) },
  { key: "ann_volatility", label: "Volatility (ann.)", format: (v) => pct(v, 1) },
  { key: "sharpe", label: "Sharpe", format: (v) => (v == null ? "—" : v.toFixed(2)) },
  { key: "max_drawdown", label: "Max drawdown", format: (v) => pct(v, 1) },
  { key: "beta_to_spy", label: "Beta to SPY", format: (v) => (v == null ? "—" : v.toFixed(2)) },
];

/** Metrics down the side, one column per series (same order and colors as the chart). */
export function StatsTable(props: { stats: Record<string, Record<string, number | string | null>>; colors?: string[] }) {
  const series = Object.keys(props.stats);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th />
            {series.map((s, i) => (
              <th className="num" key={s}>
                {props.colors?.[i] && <span className="swatch" style={{ background: props.colors[i] }} />}
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {STAT_ROWS.filter((r) => series.some((s) => props.stats[s]?.[r.key] !== undefined)).map((r) => (
            <tr key={r.key}>
              <td>{r.label}</td>
              {series.map((s) => {
                const v = props.stats[s]?.[r.key];
                return (
                  <td className="num" key={s}>
                    {r.format(typeof v === "number" ? v : null)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
