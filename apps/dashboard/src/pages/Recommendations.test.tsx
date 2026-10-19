import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { initialLive } from "../live";
import Research from "./Research";
import { JobsCard } from "./System";

function stub(responses: Record<string, unknown>) {
  const calls: { path: string; method: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((path: string, init?: RequestInit) => {
      calls.push({ path, method: init?.method ?? "GET" });
      const body = responses[path.split("?")[0] ?? ""];
      return Promise.resolve(body ? new Response(JSON.stringify(body)) : new Response("{}", { status: 404 }));
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const idea = (symbol: string, isNew: boolean) => ({
  symbol,
  kind: "swing",
  score: 0.8,
  spot: 100,
  setup: { direction: "bullish", tags: [], rs_3m: 0.02 },
  stock: { price: 100 },
  vol: { iv30: 0.3, hv20: 0.2, iv_hv: 1.5, rv_forecast: 0.22, iv_rv: 1.4, rank: null, percentile: null, history_days: 5, event: null, rich: false, as_of: null },
  pick: null,
  alternative: null,
  why: ["Uptrend."],
  risks: [],
  held: null,
  new: isNew,
});

const research = {
  run: { run_id: "r2", as_of: "2026-01-09T20:52:00Z", bars_session: "2026-01-08", previous_run: "r1", dropped: { swing: ["OLD"] } },
  as_of: "2026-01-09T20:52:00Z",
  chains_as_of: null,
  scanned: 12,
  with_chains: 5,
  without_chains: [],
  iv_history_days: 30,
  swing: [idea("XYZ", true), idea("ABC", false)],
  leaps: [],
  bearish: [],
  income: [],
  disclaimer: "Not investment advice.",
};

describe("Options ideas from the stored runs", () => {
  it("says when they were made, marks what's new, and updates on request", async () => {
    const calls = stub({ "/api/research": research, "/api/research/run": research });
    render(<Research live={initialLive} sub="ideas" detail={null} />);
    expect(await screen.findByText("1 new since the previous session")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Swing calls \(2, 1 new\)/ })).toBeInTheDocument();
    expect(screen.getByText(/No longer listed since the previous session: OLD/)).toBeInTheDocument();
    const xyz = screen.getByRole("link", { name: "XYZ" }).closest("article")!;
    expect(within(xyz).getByText("new")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Update now" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/research/run" && c.method === "POST")).toBe(true));
  });

  it("shows the track record by list and per recommendation", async () => {
    stub({
      "/api/research/track-record": {
        as_of: "2026-01-09",
        summary: [
          { list: "swing", name: "Swing calls", recommendations: 3, marked: 2, profitable: 0.5, median_result: 0.12, right_way_5d: 0.67, right_way_21d: 0.5, median_move_5d: 0.01, median_move_21d: 0.03, beat_spy: 0.33 },
        ],
        recommendations: [
          {
            list: "swing", symbol: "XYZ", structure: "long call", recommended_at: "2025-12-01T21:00:00+00:00", last_seen: "2025-12-03T21:00:00+00:00",
            runs: 3, rank: 1, spot: 100, entry: 2, move: 0.08, move_vs_spy: 0.05, move_5d: 0.02, move_21d: 0.06, value: 3.1, pnl: 110, result: 0.55,
            status: "open", marked: "2026-01-09",
            legs: [{ contract: "XYZ260116C00100000", label: "XYZ Jan 16 '26 $100 Call", side: "buy", price: 2, expiration: "2026-01-16", strike: 100, right: "call" }],
          },
        ],
      },
    });
    render(<Research live={initialLive} sub="track-record" detail={null} />);
    const row = (await screen.findByRole("cell", { name: "Swing calls" })).closest("tr")!;
    expect(row).toHaveTextContent("50% of 2");
    expect(screen.getByText(/bought XYZ Jan 16 '26 \$100 Call/)).toBeInTheDocument();
    expect(screen.getByText(/listed 3 times/)).toBeInTheDocument();
    expect(screen.getByText("+$110.00")).toBeInTheDocument();
  });
});

describe("Scheduled jobs", () => {
  it("lists each job's schedule and last run, and runs one on request", async () => {
    const status = {
      running: true, pid: 42, heartbeat: "2026-01-09T20:52:00Z", here: true,
      jobs: [
        { name: "bars", title: "Daily bars", when: "trading days from 18:30 to the next open", command: "tp-data bars daily", off: false, blocked: null,
          elsewhere: "cron", last: { status: "ok", message: "done", finished_at: "2026-01-08T23:31:00Z" }, next: "2026-01-09T23:30:00Z", open_now: false },
        { name: "fundamentals", title: "Fundamentals (SEC)", when: "trading days from 20:00", command: "tp-data fundamentals", off: false,
          blocked: "needs TP_SEC_USER_AGENT in .env", elsewhere: null, last: null, next: null, open_now: false },
        { name: "recommendations", title: "Recommendations", when: "whenever new data lands", command: "tp-recs run", off: false, blocked: null,
          elsewhere: null, last: { status: "gave up", message: "exit 1: ValueError: odd", finished_at: "2026-01-09T20:00:00Z" }, next: null, open_now: false },
      ],
    };
    const calls = stub({ "/api/jobs": status, "/api/jobs/bars/run": status });
    render(<JobsCard />);
    expect(await screen.findByText(/scheduler running in this server/)).toBeInTheDocument();
    expect(screen.getByText("also in cron")).toBeInTheDocument();
    expect(screen.getByText("needs TP_SEC_USER_AGENT in .env")).toBeInTheDocument();
    expect(screen.getByText("exit 1: ValueError: odd")).toBeInTheDocument();
    const bars = screen.getByText("Daily bars").closest("tr")!;
    fireEvent.click(within(bars).getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/jobs/bars/run" && c.method === "POST")).toBe(true));
    expect(within(bars).getByRole("button", { name: "Started" })).toBeDisabled();
  });
});

describe("Models", () => {
  it("shows each model's version, out-of-sample score, drift and live accuracy", async () => {
    stub({
      "/api/models": {
        models: [
          {
            model: "ml-rank", title: "Cross-sectional ranking model (top names)", status: "warn", versions: 2, checked_at: "2026-01-09T00:30:00Z",
            reasons: ["inputs unlike its training data: vol_21"],
            version: { version: "20251001-ab12cd34", cutoff: "2025-10-01", trained_at: "2025-10-01T23:00:00Z", trained_to: "2025-09-02", examples: 60, validation: { rank_ic: 0.041 } },
            drift: [{ feature: "vol_21", psi: 0.31, level: "shifted" }],
            live: { dates: 7, mean_ic: 0.02 },
          },
          { model: "rsi2-meta", status: "never run", versions: 0 },
        ],
      },
    });
    const { ModelsCard } = await import("./System");
    render(<ModelsCard />);
    expect(await screen.findByText("inputs unlike its training data: vol_21")).toBeInTheDocument();
    expect(screen.getByText("rank IC +0.041")).toBeInTheDocument();
    expect(screen.getByText("vol_21 shifted")).toBeInTheDocument();
    expect(screen.getByText("over 7 periods")).toBeInTheDocument();
    expect(screen.getByText("never run")).toBeInTheDocument();
  });
});
