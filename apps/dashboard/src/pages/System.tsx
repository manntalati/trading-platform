import { useApi } from "../api";
import { Card, ErrorBanner, Loading } from "../components/ui";
import { issueSummary, timeAgo } from "../format";
import type { LiveState } from "../live";
import type { Status } from "../types";

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

export default function System({ live }: { live: LiveState }) {
  const status = useApi<Status>("/api/status", 15_000);
  const s = status.data;
  if (status.loading) return <Loading />;
  return (
    <div className="grid">
      <ErrorBanner error={status.error} />
      {s && (
        <Card title="Pipeline health">
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
                label="Brokerage sync"
                ok={s.broker.length ? true : null}
                detail={s.broker.map((b) => `${b.institution} ${b.account}: ${timeAgo(b.synced_at)}`).join(" · ") || "not linked"}
              />
              <Row
                label="Live quotes"
                ok={live.connection === "open" && !live.error}
                detail={`${s.live.source} feed · ${s.live.symbols.length} symbols · socket ${live.connection}${live.error ? ` · ${live.error}` : ""}`}
              />
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
