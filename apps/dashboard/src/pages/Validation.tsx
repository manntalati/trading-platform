import { useEffect, useState } from "react";
import { postJson, useApi } from "../api";
import { Card, Delta, Empty, ErrorBanner, Loading } from "../components/ui";
import { num, pct, timeAgo } from "../format";
import type { GateCheck, PeriodStats, StrategyValidation, Validation, ValidationReport } from "../types";

const CHECK_HINT: Record<string, string> = {
  "Locked test set": "positive after costs on recent data nothing was tuned on",
  "Walk-forward": "positive when the parameters are re-chosen as it goes",
  "Deflated Sharpe": "beats what the best of every variant tried would show by luck",
  "Stress periods": "survives each crash in the data",
  "Parameter stability": "nearby parameters keep at least half the Sharpe",
};

/** The plan's stage-1 gate for every strategy: what it takes to earn paper money, and where each
 * stands. */
export default function ValidationView() {
  const [running, setRunning] = useState(false);
  const data = useApi<Validation>("/api/strategies/validation", running ? 3000 : 60_000);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const v = data.data;
  const job = v?.job;
  useEffect(() => {
    if (job) setRunning(job.running);
  }, [job]);

  async function run(names?: string[]) {
    setError(null);
    try {
      await postJson("/api/strategies/validation", { names: names ?? null });
      setRunning(true);
      data.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (data.loading && !v) return <Loading />;
  if (!v) return <ErrorBanner error={data.error} />;
  const current = v.strategies.find((s) => s.name === selected) ?? null;
  const unproven = v.strategies.filter((s) => s.on_paper && !s.report?.gate.passed);
  return (
    <div className="grid">
      <Card
        title="Backtest gate"
        hint={`stage 1 of the plan: a strategy earns paper money only after passing all five · test set from ${v.periods.validation_end}`}
      >
        <div className="toolbar">
          <button className="primary" disabled={job?.running} onClick={() => run()}>
            {job?.running ? `Validating ${job.current ?? ""}… (${job.done?.length ?? 0}/${job.names?.length ?? 0})` : "Validate all"}
          </button>
          {job?.finished_at && !job.running && (
            <span className="small muted">
              <span className={`dot ${job.ok ? "good" : "critical"}`} aria-hidden="true" /> {job.message} · {timeAgo(job.finished_at)}
            </span>
          )}
          <span className="small muted">or from a terminal: <code>uv run tp-backtest validate --all</code></span>
        </div>
        <ErrorBanner error={error} />
        {unproven.length > 0 && (
          <div className="banner">
            Paper trading {unproven.length} strateg{unproven.length === 1 ? "y that hasn't" : "ies that haven't"} passed the backtest gate:{" "}
            {unproven.map((s) => s.name).join(", ")}. Their paper results are still useful, but by the plan they're experiments until they pass.
          </div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Stage</th>
                <th>Gate</th>
                <th className="num">Test Sharpe</th>
                <th className="num">Walk-forward</th>
                <th className="num">Real (DSR)</th>
                <th className="num">Full CAGR</th>
                <th>Validated</th>
              </tr>
            </thead>
            <tbody>
              {v.strategies.map((s) => (
                <Row key={s.name} s={s} selected={selected === s.name} onSelect={() => setSelected(selected === s.name ? null : s.name)} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {current &&
        (current.report ? (
          <ReportDetail report={current.report} onRerun={() => run([current.name])} busy={job?.running ?? false} />
        ) : (
          <Card title={current.title}>
            <Empty>
              Not validated yet. <button className="link" onClick={() => run([current.name])}>Validate it now</button>
            </Empty>
          </Card>
        ))}
    </div>
  );
}

function Row({ s, selected, onSelect }: { s: StrategyValidation; selected: boolean; onSelect: () => void }) {
  const r = s.report;
  const passed = r?.gate.checks.filter((c) => c.passed === true).length ?? 0;
  return (
    <tr className={`clickable ${selected ? "selected" : ""}`} onClick={onSelect}>
      <td>
        <button className="link strong" onClick={onSelect}>
          {s.title}
        </button>
        <div className="small muted">{s.name}</div>
      </td>
      <td>{s.on_paper ? <span className="pill">paper</span> : <span className="muted">backtest</span>}</td>
      <td>
        {!r ? (
          <span className="muted">not run</span>
        ) : r.gate.passed ? (
          <span>
            <span className="dot good" aria-hidden="true" /> passed
          </span>
        ) : (
          <span>
            <span className="dot critical" aria-hidden="true" /> {passed} of {r.gate.checks.length}
          </span>
        )}
      </td>
      <td className="num">{num(r?.periods.test.sharpe, 2)}</td>
      <td className="num">{num(r?.walk_forward.sharpe, 2)}</td>
      <td className="num">{pct(r?.deflated.probability, 0)}</td>
      <td className="num">{r ? <Delta value={r.full.cagr} digits={1} /> : "—"}</td>
      <td className="muted small">{r ? timeAgo(r.created_at) : "—"}</td>
    </tr>
  );
}

function Check({ c }: { c: GateCheck }) {
  const state = c.passed === null ? "" : c.passed ? "good" : "critical";
  return (
    <div className={`note idea ${c.passed === null ? "info" : c.passed ? "info" : "attention"}`}>
      <strong>
        <span className={`dot ${state}`} aria-hidden="true" /> {c.name}: {c.passed === null ? "can't tell yet" : c.passed ? "pass" : "fail"}
      </strong>{" "}
      <span className="muted small">{CHECK_HINT[c.name]}</span>
      <div>{c.detail}</div>
    </div>
  );
}

function ReportDetail({ report: r, onRerun, busy }: { report: ValidationReport; onRerun: () => void; busy: boolean }) {
  const mc = r.monte_carlo;
  return (
    <div className="grid">
      <Card
        title={
          <>
            <strong className="detail-title">{r.title}</strong>
            <span className="hint">
              {Object.entries(r.params)
                .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : String(v)}`)
                .join(" · ")}
            </span>
          </>
        }
        hint={undefined}
      >
        <div className="toolbar">
          <span className={`pill`}>
            <span className={`dot ${r.gate.passed ? "good" : "critical"}`} aria-hidden="true" /> {r.gate.passed ? "Ready for paper trading" : "Not through the gate"}
          </span>
          <span className="small muted">validated {timeAgo(r.created_at)}</span>
          <button className="link" onClick={onRerun} disabled={busy}>
            Validate again
          </button>
          {r.test_touches > 1 && <span className="small muted">the locked test set has been looked at {r.test_touches} times for these parameters</span>}
        </div>
        <div className="notes">
          {r.gate.checks.map((c) => (
            <Check key={c.name} c={c} />
          ))}
        </div>
      </Card>
      <div className="grid cols-2 top">
        <Card title="Periods" hint="after costs, against buy-and-hold SPY">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Period</th>
                  <th className="num">CAGR</th>
                  <th className="num">Sharpe</th>
                  <th className="num">Max DD</th>
                  <th className="num">SPY CAGR</th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ["Train", r.periods.train],
                    ["Validation", r.periods.validation],
                    ["Locked test", r.periods.test],
                    ["Whole run", r.full],
                  ] as [string, PeriodStats][]
                ).map(([label, p]) => (
                  <tr key={label}>
                    <td>
                      {label}
                      <div className="small muted">{p.start ? `${p.start} → ${p.end}` : "no data"}</div>
                    </td>
                    <td className="num">{pct(p.cagr, 1)}</td>
                    <td className="num">{num(p.sharpe, 2)}</td>
                    <td className="num">{pct(p.max_drawdown, 1)}</td>
                    <td className="num muted">{pct(p.benchmark?.cagr, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="Stress periods" hint="drawdown inside each crash">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Period</th>
                  <th className="num">Strategy</th>
                  <th className="num">SPY</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {r.stress.map((s) => (
                  <tr key={s.name}>
                    <td>
                      {s.name}
                      <div className="small muted">
                        {s.start} → {s.end}
                      </div>
                    </td>
                    <td className="num">{pct(s.max_drawdown, 1)}</td>
                    <td className="num muted">{pct(s.benchmark_max_drawdown, 1)}</td>
                    <td>{s.passed === null ? <span className="muted">{s.note ?? "n/a"}</span> : s.passed ? "survived" : "worse than SPY"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="By market regime" hint="annualized return on the days in each, vs SPY">
          {Object.keys(r.regimes).length ? (
            <div className="table-wrap">
              <table>
                <tbody>
                  {(["trend", "volatility", "breadth", "rates", "curve"] as const).flatMap((dim) =>
                    (r.regimes[dim] ?? []).map((row, i) => (
                      <tr key={`${dim}-${row.label}`}>
                        <td className="muted">{i === 0 ? dim : ""}</td>
                        <td>{row.label}</td>
                        <td className="num">
                          <Delta value={row.ann_return} digits={1} />
                        </td>
                        <td className="num muted">SPY {pct(row.benchmark_ann_return, 1)}</td>
                        <td className="num muted small">{pct(row.share, 0)} of days</td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty>Needs SPY bars.</Empty>
          )}
        </Card>
        <Card title="What else could have happened" hint={mc.paths ? `${mc.paths} resampled paths of its own daily returns` : undefined}>
          {mc.paths ? (
            <div className="metrics">
              <div className="metric">
                <div className="label">CAGR, 5th to 95th percentile</div>
                <div className="value">
                  {pct(mc.cagr_p5, 1)} to {pct(mc.cagr_p95, 1)}
                </div>
                <div className="sub">median {pct(mc.cagr_p50, 1)}</div>
              </div>
              <div className="metric">
                <div className="label">Bad-tail drawdown</div>
                <div className="value">{pct(mc.max_drawdown_p5, 1)}</div>
                <div className="sub">1 path in 20 is worse</div>
              </div>
              <div className="metric">
                <div className="label">Paths losing money</div>
                <div className="value">{pct(mc.prob_loss, 0)}</div>
              </div>
            </div>
          ) : (
            <Empty>Too short to resample.</Empty>
          )}
        </Card>
      </div>
      <Card title="Parameter stability and walk-forward" hint={`walk-forward efficiency ${num(r.walk_forward.efficiency, 2)} (1 = no decay out of sample)`}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Parameters</th>
                <th className="num">Sharpe</th>
                <th className="num">CAGR</th>
                <th className="num">Max DD</th>
                <th className="num">Walk-forward picks</th>
              </tr>
            </thead>
            <tbody>
              {r.stability.variants.map((v) => {
                const key = JSON.stringify(v.params);
                const picks = r.walk_forward.windows.filter((w) => JSON.stringify(w.params) === key).length;
                return (
                  <tr key={key} className={v.base ? "selected" : undefined}>
                    <td className="small">{changed(v.params, r.params) || "chosen parameters"}</td>
                    <td className="num">{num(v.sharpe, 2)}</td>
                    <td className="num">{pct(v.cagr, 1)}</td>
                    <td className="num">{pct(v.max_drawdown, 1)}</td>
                    <td className="num muted">{picks || ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

/** "lookback=189" for a variant that differs from the chosen parameters in one place. */
function changed(params: Record<string, unknown>, base: Record<string, unknown>): string {
  return Object.entries(params)
    .filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(base[k]))
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
}
