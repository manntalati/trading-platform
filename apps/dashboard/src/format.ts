const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function money(value: number | null | undefined, cents = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return (cents || Math.abs(value) < 1000 ? usd2 : usd0).format(value);
}

/** Signed money, e.g. "+$12.30" / "−$4.00" (true minus sign for readability). */
export function signedMoney(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const body = usd2.format(Math.abs(value));
  return `${value >= 0 ? "+" : "−"}${body}`;
}

export function pct(value: number | null | undefined, digits = 1, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const body = `${(Math.abs(value) * 100).toFixed(digits)}%`;
  if (!signed) return value < 0 ? `−${body}` : body;
  return `${value >= 0 ? "+" : "−"}${body}`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function qty(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

export function timeAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "never";
  const seconds = Math.round((now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function nyTime(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Validation counts {"warning:zero_volume": 3} -> "3 warning: zero volume". */
export function issueSummary(counts: Record<string, number> | null | undefined): string {
  const entries = Object.entries(counts ?? {});
  if (!entries.length) return "no issues";
  return entries.map(([key, n]) => `${n} ${key.replace(":", ": ").replaceAll("_", " ")}`).join(", ");
}
