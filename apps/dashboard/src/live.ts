import { useEffect, useReducer } from "react";
import { dashboardToken } from "./api";
import type { LiveMessage, LivePortfolio, Quote } from "./types";

export type Connection = "connecting" | "open" | "closed";

export interface LiveState {
  connection: Connection;
  quotes: Record<string, Quote>;
  portfolio: LivePortfolio | null;
  source: string | null;
  error: string | null;
  lastUpdate: string | null;
}

export const initialLive: LiveState = {
  connection: "connecting",
  quotes: {},
  portfolio: null,
  source: null,
  error: null,
  lastUpdate: null,
};

export type LiveAction =
  | { type: "message"; message: LiveMessage }
  | { type: "connection"; connection: Connection };

/** Pure state transition, so it can be unit-tested without a socket. */
export function liveReducer(state: LiveState, action: LiveAction): LiveState {
  if (action.type === "connection") return { ...state, connection: action.connection };
  const m = action.message;
  const quotes = m.type === "snapshot" ? {} : { ...state.quotes };
  for (const q of m.quotes) quotes[q.symbol] = q;
  return {
    ...state,
    connection: "open",
    quotes,
    portfolio: m.portfolio ?? state.portfolio,
    source: m.feed.source,
    error: m.feed.error,
    lastUpdate: m.at ?? state.lastUpdate,
  };
}

/** One WebSocket per tab with exponential-backoff reconnect. */
export function useLive(): LiveState {
  const [state, dispatch] = useReducer(liveReducer, initialLive);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let stopped = false;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const token = dashboardToken();
      const query = token ? `?token=${encodeURIComponent(token)}` : "";
      socket = new WebSocket(`${proto}://${window.location.host}/ws/live${query}`);
      dispatch({ type: "connection", connection: "connecting" });
      socket.onopen = () => {
        retry = 0;
      };
      socket.onmessage = (event) => {
        try {
          dispatch({ type: "message", message: JSON.parse(String(event.data)) as LiveMessage });
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        dispatch({ type: "connection", connection: "closed" });
        if (stopped) return;
        const delay = Math.min(30_000, 1000 * 2 ** retry++);
        timer = window.setTimeout(connect, delay);
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return state;
}
