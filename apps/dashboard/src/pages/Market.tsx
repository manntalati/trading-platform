import { useState } from "react";
import { useApi } from "../api";
import { PriceChart } from "../components/charts";
import { Card, Delta, Empty, ErrorBanner, Loading } from "../components/ui";
import { money } from "../format";
import type { LiveState } from "../live";
import type { Bars } from "../types";

const RANGES = [
  { label: "3M", days: 63 },
  { label: "1Y", days: 252 },
  { label: "5Y", days: 1260 },
];

/** Daily price history of one symbol, with the live price when it's streamed. */
export function PriceHistory({ symbol, live }: { symbol: string; live: LiveState }) {
  const [days, setDays] = useState(252);
  const bars = useApi<Bars>(`/api/bars/${encodeURIComponent(symbol)}?days=${days}`);
  const quote = live.quotes[symbol];
  const data = (bars.data?.bars ?? []).map((b) => ({ date: b.date.slice(0, 10), close: b.adj_close }));
  const first = data[0]?.close;
  const last = data[data.length - 1]?.close;
  return (
    <Card title={`${symbol} price`} hint="daily, split- and dividend-adjusted">
      <div className="toolbar">
        {RANGES.map((r) => (
          <button key={r.label} aria-pressed={days === r.days} onClick={() => setDays(r.days)}>
            {r.label}
          </button>
        ))}
        {quote && (
          <span className="small">
            Live {money(quote.price, true)} <Delta value={quote.change_pct} />
          </span>
        )}
        {first && last ? (
          <span className="small muted">
            Over range <Delta value={last / first - 1} />
          </span>
        ) : null}
      </div>
      {bars.error && !bars.error.includes("no clean bars") && <ErrorBanner error={bars.error} />}
      {bars.loading ? <Loading /> : data.length ? <PriceChart data={data} /> : <Empty>No daily bars for {symbol} yet.</Empty>}
    </Card>
  );
}
