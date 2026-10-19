import { useState } from "react";
import { ApiError, postJson, useApi } from "../api";
import { Card, ErrorBanner, Loading } from "../components/ui";
import { issueSummary, nyTime, timeAgo } from "../format";
import type { LiveState } from "../live";
import type { DataMap, JobInfo, JobsStatus, Status } from "../types";

function Row(props: { label: string; ok: boolean | null; detail: string }) {
  const state = props.ok === null ? "" : props.ok ? "good" : "critical";
  return (
    <tr>
      <td>
        <span className={`dot ${state}`} aria-hidden="true" /> {props.label}
      </td>
      <td>{props.ok === null ? "n/a" : props.ok ? "OK" : "Check"}</td>
      <td className="muted">{props.detail}</td>
    </tr>
  );
}

const FLOWS = [
  { from: "Fidelity", via: "SnapTrade → tp-broker sync", to: "Portfolio, Trades, Home" },
  { from: "Alpaca market data", via: "tp-data bars daily / options snapshot", to: "Portfolio stats, Research, Paper trading" },
  { from: "SEC EDGAR", via: "tp-data fundamentals", to: "Research (earnings dates, valuation), factor and earnings strategies" },
  { from: "Cboe, the Treasury (or FRED)", via: "tp-data macro daily", to: "Home (VIX, rates), validation regimes, option pricing" },
  { from: "Paper strategies", via: "tp-paper bot → Alpaca paper account", to: "Paper trading, Trades" },
  { from: "Your questions", via: "Anthropic API (only when you ask Claude)", to: "the chat on a position or idea" },
];

export function macroDetail(s: Status): string {
  const series = s.macro?.series ?? {};
  const vix = series.VIXCLS;
  const r10 = series.DGS10;
  if (!vix && !r10) return "none yet: run tp-data macro backfill";
  const parts = [];
  if (vix?.value != null) parts.push(`VIX ${vix.value.toFixed(1)}`);
  if (r10?.value != null) parts.push(`10-year ${r10.value.toFixed(2)}%`);
  const failed = Object.keys(s.macro?.latest_report?.errors ?? {});
  return `${parts.join(" · ")} · as of ${(vix ?? r10)?.date}${failed.length ? ` · last run failed for ${failed.join(", ")}` : ""}`;
}

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

/** Where every dataset lives on disk, what writes it, what reads it, and how fresh it is. */
function DataMapCard() {
  const data = useApi<DataMap>("/api/system/data", 60_000);
  const d = data.data;
  return (
    <Card title="Where your data lives" hint={d ? <>under <code>{d.root}</code>, on this machine only</> : undefined}>
      <ErrorBanner error={data.error} />
      <div className="flows">
        {FLOWS.map((f) => (
          <div key={f.from} className="flow">
            <strong>{f.from}</strong>
            <span className="muted"> → {f.via} → </span>
            <span>{f.to}</span>
          </div>
        ))}
      </div>
      {d && (
        <div className="datasets">
          {d.datasets.map((x) => (
            <div key={x.name} className="dataset">
              <div>
                <strong>{x.name}</strong>{" "}
                <span className="muted small">
                  {x.updated_at ? `updated ${timeAgo(x.updated_at)}` : "never written"}
                  {x.files ? ` · ${bytes(x.bytes)} in ${x.files} file${x.files === 1 ? "" : "s"}` : ""}
                </span>
              </div>
              <div className="small">
                <code>{x.path}</code>
                {x.also && (
                  <>
                    {" "}
                    <code>{x.also}</code>
                  </>
                )}
              </div>
              <div className="small muted">
                Written by {x.writer}. Used by {x.used_by}.
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export default function System({ live }: { live: LiveState }) {
  const status = useApi<Status>("/api/status", 15_000);
  const s = status.data;
  if (status.loading) return <Loading />;
  return (
    <div className="grid">
      <ErrorBanner error={status.error} />
      {s && (
        <Card title="Is everything fresh?" hint="each job's last run">
          <div className="table-wrap">
          <table>
            <tbody>
              <Row
                label="Daily bars"
                ok={s.bars.latest_session ? !s.bars.stale : false}
                detail={s.bars.latest_session ? `latest ${s.bars.latest_session} (expected ${s.bars.expected_session}), ${s.bars.rows.toLocaleString()} rows, ${s.bars.symbols} symbols` : "no data: run tp-data bars backfill"}
              />
              <Row
                label="Validation"
                ok={s.validation ? Boolean(s.validation.ok) : null}
                detail={s.validation ? `${issueSummary(s.validation.counts)} · ${timeAgo(String(s.validation.generated_at ?? ""))}` : "no report yet"}
              />
              <Row
                label="Option snapshots"
                ok={s.options.latest_report ? Boolean(s.options.latest_report.ok) : null}
                detail={s.options.latest_report ? `last ${s.options.latest_report.snapshot_date} · ${s.options.files} files stored` : "none yet"}
              />
              <Row
                label="Fundamentals (SEC)"
                ok={s.fundamentals ? Boolean(s.fundamentals.ok) : null}
                detail={
                  s.fundamentals
                    ? `${Object.keys(s.fundamentals.facts ?? {}).length} companies${Object.keys(s.fundamentals.errors ?? {}).length ? ` · failed: ${Object.keys(s.fundamentals.errors ?? {}).join(", ")}` : ""}`
                    : "none yet: set TP_SEC_USER_AGENT and run tp-data fundamentals"
                }
              />
              <Row
                label="VIX and Treasury yields"
                ok={s.macro?.latest_report ? Boolean(s.macro.latest_report.ok) : null}
                detail={macroDetail(s)}
              />
              <Row
                label="Brokerage sync"
                ok={s.broker.length ? true : null}
                detail={s.broker.map((b) => `${b.institution} ${b.account}: ${timeAgo(b.synced_at)}`).join(" · ") || "not linked"}
              />
              {s.dashboard && (
                <Row
                  label="Dashboard build"
                  ok={!s.dashboard.stale}
                  detail={
                    s.dashboard.stale
                      ? "older than its source: restart tp-api (it rebuilds when Node is installed) or run make web-build"
                      : `built ${timeAgo(s.dashboard.built_at)}`
                  }
                />
              )}
              <Row
                label="Live quotes"
                ok={live.connection === "open" && !live.error}
                detail={`${s.live.source} feed · ${s.live.symbols.length} symbols · socket ${live.connection}${live.error ? ` · ${live.error}` : ""}`}
              />
            </tbody>
          </table>
          </div>
        </Card>
      )}
      <JobsCard />
      <DataMapCard />
    </div>
  );
}

const JOB_STATE: Record<string, { tone: string; text: string }> = {
  ok: { tone: "good", text: "ran" },
  fresh: { tone: "good", text: "up to date" },
  failed: { tone: "warning", text: "failed, retrying" },
  "gave up": { tone: "critical", text: "failed" },
};

/** The job scheduler: what runs when, how each job's last run went, and a button to run one now. */
export function JobsCard() {
  const res = useApi<JobsStatus>("/api/jobs", 30_000);
  const [error, setError] = useState<string | null>(null);
  const [asked, setAsked] = useState<string | null>(null);
  const s = res.data;
  async function runNow(name: string) {
    setError(null);
    setAsked(name);
    try {
      await postJson<JobsStatus>(`/api/jobs/${encodeURIComponent(name)}/run`, {});
      window.setTimeout(res.reload, 3000);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }
  return (
    <Card
      title="Scheduled jobs"
      hint={
        s ? (
          s.running ? (
            <span>
              <span className="dot good" aria-hidden="true" /> scheduler running{s.here ? " in this server" : ` (pid ${s.pid})`}
            </span>
          ) : (
            <span>
              <span className="dot warning" aria-hidden="true" /> scheduler not running: <code>uv run tp-api</code> runs it, or <code>uv run tp-jobs run</code>
            </span>
          )
        ) : null
      }
    >
      <ErrorBanner error={res.error ?? error} />
      {!s ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>When (New York)</th>
                <th>Last run</th>
                <th>Next</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {s.jobs.map((j) => (
                <JobRow key={j.name} job={j} onRun={() => runNow(j.name)} asked={asked === j.name} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">
        A job runs once in each window, as soon as the window opens or the computer is next awake inside it, so a laptop that slept through the evening still
        gets its bars. Output goes to <code>data/logs/&lt;job&gt;.log</code>. <code>TP_JOBS_OFF=options-midday,validation</code> in <code>.env</code> turns jobs off.
      </p>
    </Card>
  );
}

function JobRow({ job, onRun, asked }: { job: JobInfo; onRun: () => void; asked: boolean }) {
  const last = job.last;
  const state = job.off ? { tone: "", text: "off" } : job.blocked ? { tone: "", text: job.blocked } : last ? JOB_STATE[last.status] : { tone: "", text: "not run yet" };
  return (
    <tr>
      <td>
        {job.title}
        <div className="muted small mono">{job.command}</div>
      </td>
      <td className="small">
        {job.when}
        {job.elsewhere && <div className="muted">also in {job.elsewhere}</div>}
      </td>
      <td className="small">
        <span className={`dot ${state?.tone ?? ""}`} aria-hidden="true" /> {state?.text}
        {last && <span className="muted"> · {timeAgo(last.finished_at)}</span>}
        {last && last.status !== "ok" && last.status !== "fresh" && <div className="muted">{last.message}</div>}
      </td>
      <td className="small">{job.off || job.blocked ? "—" : job.open_now ? "due now" : job.next ? nyTime(job.next) : "—"}</td>
      <td>
        {!job.off && !job.blocked && (
          <button onClick={onRun} disabled={asked}>
            {asked ? "Started" : "Run now"}
          </button>
        )}
      </td>
    </tr>
  );
}
