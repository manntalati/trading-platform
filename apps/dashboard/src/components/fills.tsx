import { useState, type FormEvent } from "react";
import { ApiError, deleteJson, postJson, useApi } from "../api";
import { money, nyTime } from "../format";
import type { FillLeg, FillsView, HeldForFill, Instrument, RecordedFill } from "../types";

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

const KINDS: { key: Instrument; label: string }[] = [
  { key: "stock", label: "Stock" },
  { key: "call", label: "Call" },
  { key: "put", label: "Put" },
];

/** A badge that says at a glance whether a leg is shares or an option, and which. */
export function InstrumentPill({ kind }: { kind: Instrument }) {
  return <span className={`pill instrument ${kind}`}>{kind === "stock" ? "Stock" : kind === "call" ? "Call" : "Put"}</span>;
}

function count(quantity: number, unit: "shares" | "contracts"): string {
  const n = Math.abs(quantity);
  return `${n} ${n === 1 ? unit.slice(0, -1) : unit}`;
}

function LegLine({ leg }: { leg: FillLeg }) {
  const kind = leg.kind ?? "stock";
  const unit = leg.unit ?? (kind === "stock" ? "shares" : "contracts");
  const name = (leg.label ?? leg.symbol).replace(/ shares$/, "");
  return (
    <>
      <InstrumentPill kind={kind} /> {leg.quantity > 0 ? "bought" : "sold"} {count(leg.quantity, unit)} of <strong>{name}</strong>
      {leg.price ? ` at ${money(leg.price, true)}${kind === "stock" ? "" : " a share"}` : ""}
    </>
  );
}

function strikeText(k: number): string {
  return k.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** The trade as it will be recorded, in words, with what it pays or brings in. */
function preview(side: "sell" | "buy", kind: Instrument, symbol: string, expiration: string, strike: string, quantity: string, price: string): string | null {
  const q = Number(quantity);
  const p = Number(price);
  if (!symbol.trim() || !(q > 0) || !(p >= 0) || price === "") return null;
  const ticker = symbol.trim().toUpperCase();
  let what = `${count(q, "shares")} of ${ticker}`;
  let total = q * p;
  if (kind !== "stock") {
    if (!expiration || !(Number(strike) > 0)) return null;
    const day = new Date(`${expiration}T12:00:00`);
    const when = day.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" }).replace(/, (\d\d)$/, " '$1");
    what = `${count(q, "contracts")} of ${ticker} ${when} $${strikeText(Number(strike))} ${kind === "call" ? "Call" : "Put"}`;
    total = q * p * 100;
  }
  const verb = side === "sell" ? "Sold" : "Bought";
  const each = kind === "stock" ? `${money(p, true)} a share` : `${money(p, true)} a share (${money(p * 100, true)} a contract)`;
  return `${verb} ${what} at ${each}: ${money(total, true)} ${side === "sell" ? "received" : "paid"}.`;
}

/** Trades you made that Fidelity's sync doesn't show yet: record one right after trading and the
 * ideas count it at once (a sale's proceeds in the buying power, the holding gone), until the
 * sync catches up. Shares, or a call or put by its expiration and strike; a sale can be picked
 * from what you hold. Nothing is ordered here. */
export function RecordedTrades({ onChange }: { onChange?: () => void }) {
  const data = useApi<FillsView>("/api/fills", 120_000);
  const [side, setSide] = useState<"sell" | "buy">("sell");
  const [kind, setKind] = useState<Instrument>("stock");
  const [symbol, setSymbol] = useState("");
  const [expiration, setExpiration] = useState("");
  const [strike, setStrike] = useState("");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [account, setAccount] = useState("");
  const [held, setHeld] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const view = data.data;
  const shown = (view?.fills ?? []).filter((f) => f.status !== "synced" || Date.now() - Date.parse(f.at) < 3 * 86_400_000);
  const holdings = view?.holdings ?? [];
  const option = kind !== "stock";
  const line = preview(side, kind, symbol, expiration, strike, quantity, price);

  function pick(value: string) {
    setHeld(value);
    const h: HeldForFill | undefined = holdings.find((x) => `${x.account}|${x.symbol}` === value);
    if (!h) return;
    setKind(h.kind);
    setSymbol(h.underlying);
    setExpiration(h.expiration ?? "");
    setStrike(h.strike != null ? String(h.strike) : "");
    setQuantity(String(Math.abs(h.quantity)));
    setAccount(h.account ?? "");
  }

  function clear() {
    setSymbol("");
    setExpiration("");
    setStrike("");
    setQuantity("");
    setPrice("");
    setHeld("");
  }

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
    const body = {
      side,
      kind,
      symbol: symbol.trim(),
      expiration: option ? expiration : null,
      strike: option ? Number(strike) : null,
      quantity: Number(quantity),
      price: Number(price),
      account: account || null,
    };
    if (await act(() => postJson<FillsView>("/api/fills", body))) clear();
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
        <div className="fill-row">
          <div className="segmented" role="group" aria-label="Side">
            <button type="button" aria-pressed={side === "sell"} onClick={() => setSide("sell")}>
              Sold
            </button>
            <button type="button" aria-pressed={side === "buy"} onClick={() => setSide("buy")}>
              Bought
            </button>
          </div>
          <div className="segmented" role="group" aria-label="What">
            {KINDS.map((k) => (
              <button key={k.key} type="button" aria-pressed={kind === k.key} onClick={() => setKind(k.key)}>
                {k.label}
              </button>
            ))}
          </div>
          {side === "sell" && holdings.length > 0 && (
            <select aria-label="From what you hold" value={held} onChange={(e) => pick(e.target.value)}>
              <option value="">Pick from what you hold…</option>
              {holdings.map((h) => (
                <option key={`${h.account}|${h.symbol}`} value={`${h.account}|${h.symbol}`}>
                  {h.kind === "stock" ? "Stock" : h.kind === "call" ? "Call" : "Put"}: {h.label.replace(/ shares$/, "")} ({count(h.quantity, h.unit)})
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="fill-row">
          <label>
            <span>{option ? "Underlying" : "Symbol"}</span>
            <input aria-label="Symbol" placeholder="XYZ" value={symbol} onChange={(e) => setSymbol(e.target.value)} required />
          </label>
          {option && (
            <>
              <label>
                <span>Expiration</span>
                <input aria-label="Expiration" type="date" value={expiration} onChange={(e) => setExpiration(e.target.value)} required />
              </label>
              <label>
                <span>Strike</span>
                <input aria-label="Strike" inputMode="decimal" placeholder="25" value={strike} onChange={(e) => setStrike(e.target.value)} required />
              </label>
            </>
          )}
          <label>
            <span>{option ? "Contracts" : "Shares"}</span>
            <input aria-label="Quantity" inputMode="decimal" placeholder={option ? "1" : "10"} value={quantity} onChange={(e) => setQuantity(e.target.value)} required />
          </label>
          <label>
            <span>{option ? "Price a share" : "Price"}</span>
            <input
              aria-label="Price"
              inputMode="decimal"
              placeholder={option ? "1.45" : "52.10"}
              title={option ? "the option's price per share as quoted (1.45 is $145 a contract)" : "your fill per share"}
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              required
            />
          </label>
          {(view?.accounts.length ?? 0) > 1 && (
            <label>
              <span>Account</span>
              <select aria-label="Account" value={account} onChange={(e) => setAccount(e.target.value)}>
                <option value="">the one holding it, else the ideas'</option>
                {view?.accounts.map((a) => (
                  <option key={a.key} value={a.key}>
                    {a.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="submit" disabled={busy || !view?.synced}>
            {busy ? "Recording…" : "Record"}
          </button>
        </div>
        {line && <p className="small fill-preview">{line}</p>}
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
                {f.legs.map((leg, i) => (
                  <span key={leg.symbol}>
                    {i > 0 && ", "}
                    <LegLine leg={leg} />
                  </span>
                ))}{" "}
                <span className={f.cash >= 0 ? "up" : "down"}>({f.cash >= 0 ? "+" : "−"}{money(Math.abs(f.cash), true)})</span>{" "}
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
