import { initialLive, liveReducer } from "./live";
import type { LiveMessage } from "./types";

const quote = (symbol: string, price: number) => ({
  symbol,
  price,
  prev_close: 100,
  change_pct: price / 100 - 1,
  at: "2026-10-05T14:00:00Z",
  live: true,
});

describe("liveReducer", () => {
  it("replaces quotes on snapshot and merges on update", () => {
    const snapshot: LiveMessage = {
      type: "snapshot",
      quotes: [quote("SPY", 101), quote("QQQ", 99)],
      portfolio: { value: 1000, day_pnl: 5, day_pnl_pct: 0.005 },
      feed: { source: "fake", error: null },
    };
    let state = liveReducer(initialLive, { type: "message", message: snapshot });
    expect(Object.keys(state.quotes).sort()).toEqual(["QQQ", "SPY"]);
    expect(state.connection).toBe("open");

    const update: LiveMessage = {
      type: "update",
      at: "2026-10-05T14:00:01Z",
      quotes: [quote("SPY", 102)],
      portfolio: null,
      feed: { source: "fake", error: null },
    };
    state = liveReducer(state, { type: "message", message: update });
    expect(state.quotes.SPY?.price).toBe(102);
    expect(state.quotes.QQQ?.price).toBe(99);
    expect(state.portfolio?.value).toBe(1000); // kept when an update carries none
    expect(state.lastUpdate).toBe("2026-10-05T14:00:01Z");
  });

  it("tracks connection state and feed errors", () => {
    const closed = liveReducer(initialLive, { type: "connection", connection: "closed" });
    expect(closed.connection).toBe("closed");
    const errored = liveReducer(closed, {
      type: "message",
      message: { type: "update", quotes: [], portfolio: null, feed: { source: "alpaca", error: "auth failed" } },
    });
    expect(errored.error).toBe("auth failed");
  });
});
