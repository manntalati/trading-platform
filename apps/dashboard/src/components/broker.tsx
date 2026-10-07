import { useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../api";
import { nyTime, timeAgo } from "../format";
import type { BrokerFreshness, BrokerSyncState } from "../types";

/** How current the Fidelity data is, and a button to pull it now.
 *
 * Positions come through SnapTrade, which keeps a copy refreshed once a day; the button has it
 * re-pull them from Fidelity first (SnapTrade charges a small fee per refresh). Transactions
 * only ever arrive the next day, so today's trades show as "pending" from the positions. */
export function FidelityFreshness(props: { freshness?: BrokerFreshness | null; onSynced?: () => void }) {
  const [state, setState] = useState<BrokerSyncState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = state?.running ?? false;
  const onSynced = useRef(props.onSynced);
  onSynced.current = props.onSynced;

  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(async () => {
      try {
        const next = await getJson<BrokerSyncState>("/api/broker/sync");
        setState(next);
        if (!next.running) onSynced.current?.();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, 3000);
    return () => window.clearInterval(id);
  }, [running]);

  async function refresh() {
    setError(null);
    try {
      setState(await postJson<BrokerSyncState>("/api/broker/sync", { refresh: true }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const f = props.freshness;
  return (
    <div className="toolbar small">
      <span className="muted">
        {f?.positions_as_of ? <>Fidelity positions as of {when(f.positions_as_of)}</> : "Fidelity positions: not synced yet"}
        {f?.transactions_through ? <> · transactions through {f.transactions_through}</> : null}
        {f?.synced_at ? <> · synced {timeAgo(f.synced_at)}</> : null}
      </span>
      <span className="spacer" />
      {running ? (
        <span className="muted">
          <span className="dot warning pulse" aria-hidden="true" /> Refreshing from Fidelity… (up to a few minutes)
        </span>
      ) : (
        <>
          {state?.finished_at && (
            <span className="muted" title={state.message ?? undefined}>
              <span className={`dot ${state.ok ? "good" : "critical"}`} aria-hidden="true" /> {state.ok ? "Updated" : "Refresh failed"} {timeAgo(state.finished_at)}
            </span>
          )}
          <button onClick={refresh} title="Has SnapTrade re-pull your positions from Fidelity now (a small SnapTrade fee per refresh)">
            Refresh from Fidelity
          </button>
        </>
      )}
      {error && <span className="muted">{error}</span>}
    </div>
  );
}

function when(iso: string): string {
  return `${nyTime(iso)} ET`;
}
