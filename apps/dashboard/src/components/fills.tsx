import { useState, type FormEvent } from "react";
import { ApiError, deleteJson, postJson, useApi } from "../api";
import { money, nyTime } from "../format";
import type { FillsView, RecordedFill } from "../types";

const STATUS: Record<RecordedFill["status"], { label: string; tone: string; title: string }> = {
  pending: { label: "counted", tone: "warning", title: "Fidelity's sync doesn't show it yet: the ideas count it" },
  settling: {
    label: "settling",
    tone: "warning",
    title: "the sync shows the holding; its money is still counted until Fidelity's buying power does, or it settles",
  },
  synced: { label: "synced", tone: "good", title: "Fidelity's numbers have it now" },
  stale: { label: "not seen", tone: "critical", title: "the sync still doesn't show it after a week: no longer counted. Entered wrong? Remove it" },
};

function legLine(f: RecordedFill): string {
  return f.legs.map((l) => `${l.quantity > 0 ? "bought" : "sold"} ${Math.abs(l.quantity)} ${l.symbol}${l.price ? ` at ${money(l.price, true)}` : ""}`).join(", ");
}

/** Trades you made that Fidelity's sync doesn't show yet: record one right after trading and the
 * ideas count it at once (a sale's proceeds in the buying power, the holding gone), until the
 * sync catches up. Nothing is ordered here. */
export function RecordedTrades({ onChange }: { onChange?: () => void }) {
  const data = useApi<FillsView>("/api/fills", 120_000);
  const [side, setSide] = useState<"sell" | "buy">("sell");
  const [symbol, setSymbol] = useState("");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [account, setAccount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const view = data.data;
  const shown = (view?.fills ?? []).filter((f) => f.status !== "synced" || Date.now() - Date.parse(f.at) < 3 * 86_400_000);

  async function act(run: () => Promise<FillsView>) {
    setBusy(true);
    setError(null);
    try {
      await run();
      data.reload();
      onChange?.();
      return true;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const body = { side, symbol: symbol.trim(), quantity: Number(quantity), price: Number(price), account: account || null };
    if (await act(() => postJson<FillsView>("/api/fills", body))) {
      setSymbol("");
      setQuantity("");
      setPrice("");
    }
  }

  return (
    <details className="card fills" open={shown.some((f) => f.counts)}>
      <summary>
        <strong>Traded just now?</strong>{" "}
        <span className="muted small">
          Record it and the ideas count it before Fidelity's sync does: a sale's money is buying power at once, even before it settles.
        </span>
      </summary>
      <form className="fill-form" onSubmit={submit}>
        <select aria-label="Side" value={side} onChange={(e) => setSide(e.target.value as "sell" | "buy")}>
          <option value="sell">Sold</option>
          <option value="buy">Bought</option>
        </select>
        <input aria-label="Symbol" placeholder="Symbol or OCC contract" value={symbol} onChange={(e) => setSymbol(e.target.value)} required />
        <input aria-label="Quantity" placeholder="Shares or contracts" inputMode="decimal" value={quantity} onChange={(e) => setQuantity(e.target.value)} required />
        <input aria-label="Price" placeholder="Fill, per share" inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} required />
        {(view?.accounts.length ?? 0) > 1 && (
          <select aria-label="Account" value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="">Account: the one holding it, else the ideas'</option>
            {view?.accounts.map((a) => (
              <option key={a.key} value={a.key}>
                {a.label}
              </option>
            ))}
          </select>
        )}
        <button type="submit" disabled={busy || !view?.synced}>
          {busy ? "Recording…" : "Record"}
        </button>
      </form>
      {view && !view.synced && <p className="muted small">Sync the brokerage once first (tp-broker sync): a trade is counted against an account.</p>}
      {error && (
        <p className="down small" role="alert">
          {error}
        </p>
      )}
      {shown.length > 0 && (
        <ul className="fill-list">
          {shown.map((f) => {
            const s = STATUS[f.status];
            return (
              <li key={f.id}>
                <span className={`pill ${s.tone}`} title={s.title}>
                  {s.label}
                </span>{" "}
                {legLine(f)} <span className={f.cash >= 0 ? "up" : "down"}>({f.cash >= 0 ? "+" : "−"}{money(Math.abs(f.cash), true)})</span>{" "}
                <span className="muted small">
                  {f.account} · {nyTime(f.at)}
                  {f.journal_id ? ` · journal #${f.journal_id}` : ""}
                </span>{" "}
                <button className="link small" disabled={busy} onClick={() => act(() => deleteJson<FillsView>(`/api/fills/${f.id}`))} title="Entered wrong? Remove it">
                  remove
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </details>
  );
}
