import { useCallback, useEffect, useRef, useState } from "react";

/** Bearer token for a tp-api started with TP_DASHBOARD_TOKEN: open the dashboard once with
 * ?token=..., it is kept for the browser session and removed from the address bar. */
export function dashboardToken(): string | null {
  try {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("token");
    if (fromUrl) {
      sessionStorage.setItem("tp-token", fromUrl);
      url.searchParams.delete("token");
      window.history.replaceState(null, "", url.toString());
      return fromUrl;
    }
    return sessionStorage.getItem("tp-token");
  } catch {
    return null;
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const token = dashboardToken();
  const res = await fetch(path, {
    signal,
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!res.ok) throw new ApiError(res.status, detail(await res.text()) || res.statusText);
  return (await res.json()) as T;
}

/** POST JSON for actions that change state. The X-TP-Client header is how the API tells the
 * dashboard apart from a cross-site request (see tp_api/deps.py). */
export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const token = dashboardToken();
  const headers: Record<string, string> = { "Content-Type": "application/json", "X-TP-Client": "dashboard" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) throw new ApiError(res.status, detail(await res.text()) || res.statusText);
  return (await res.json()) as T;
}

/** FastAPI errors arrive as {"detail": "..."} (or a list of validation problems). */
function detail(text: string): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (Array.isArray(parsed.detail)) {
      return parsed.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ");
    }
  } catch {
    /* not JSON: use the text as is */
  }
  return text;
}

// The side panels and the page in the middle often ask for the same thing at once: requests for
// one path share a single fetch, and an answer is reused for a few seconds.
const inflight = new Map<string, Promise<unknown>>();
const recent = new Map<string, { at: number; body: unknown }>();
const REUSE_MS = 5_000;

/** GET ``path``, shared with any other caller asking for it now; ``fresh`` skips the reuse. */
export function sharedGet<T>(path: string, fresh = false): Promise<T> {
  const hit = recent.get(path);
  if (!fresh && hit && Date.now() - hit.at < REUSE_MS) return Promise.resolve(hit.body as T);
  const pending = inflight.get(path);
  if (!fresh && pending) return pending as Promise<T>;
  const request = getJson<unknown>(path)
    .then((body) => {
      recent.set(path, { at: Date.now(), body });
      return body;
    })
    .finally(() => {
      if (inflight.get(path) === request) inflight.delete(path);
    });
  inflight.set(path, request);
  return request as Promise<T>;
}

/** Forget shared answers (tests, and after an action that changes the data). */
export function clearApiCache() {
  inflight.clear();
  recent.clear();
}

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Fetch ``path`` and refetch every ``refreshMs`` (0 = never). */
export function useApi<T>(path: string | null, refreshMs = 0): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const first = useRef(true);

  useEffect(() => {
    if (path === null) return;
    let gone = false;
    if (first.current) setLoading(true);
    // a reload (tick > 0) wants new data, not a shared answer from a moment ago
    sharedGet<T>(path, tick > 0)
      .then((body) => {
        if (gone) return;
        setData(body);
        setError(null);
      })
      .catch((err: unknown) => {
        if (gone) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!gone) {
          setLoading(false);
          first.current = false;
        }
      });
    return () => {
      gone = true;
    };
  }, [path, tick]);

  useEffect(() => {
    if (!refreshMs || path === null) return;
    const id = window.setInterval(reload, refreshMs);
    return () => window.clearInterval(id);
  }, [refreshMs, path, reload]);

  return { data, error, loading, reload };
}
