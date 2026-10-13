import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { Validation, ValidationReport } from "../types";
import ValidationView from "./Validation";

const report: ValidationReport = {
  run_id: "r1",
  created_at: new Date().toISOString(),
  strategy: "ma-timing",
  title: "10-month moving-average timing",
  params: { months: 10, assets: ["SPY", "EFA"] },
  full: { sessions: 1800, cagr: 0.057, sharpe: 0.9, max_drawdown: -0.09, start: "2019-10-01", end: "2026-01-09" },
  periods: {
    train: { sessions: 980, cagr: 0.08, sharpe: 1.17, max_drawdown: -0.057, start: "2019-10-01", end: "2023-06-30", benchmark: { cagr: 0.2, sharpe: 1, max_drawdown: -0.2, total_return: 0.5 } },
    validation: { sessions: 378, cagr: 0.001, sharpe: 0.04, max_drawdown: -0.064 },
    test: { sessions: 444, cagr: 0.057, sharpe: 1.06, max_drawdown: -0.04 },
  },
  walk_forward: { windows: [{ start: "2021-10-01", end: "2022-03-31", params: { months: 12, assets: ["SPY", "EFA"] }, train_sharpe: 1, test_return: 0.02 }], sharpe: 1.02, efficiency: 0.9 },
  deflated: { sharpe: 0.9, trials: 5, threshold: 0.1, probability: 0.983, excess: 0.8 },
  stress: [
    { name: "COVID crash", start: "2020-02-19", end: "2020-03-23", passed: true, max_drawdown: -0.009, benchmark_max_drawdown: -0.049 },
    { name: "Before the data", start: "2001-01-01", end: "2001-02-01", passed: null, note: "not in the backtest" },
  ],
  stability: {
    variants: [
      { params: { months: 10, assets: ["SPY", "EFA"] }, sharpe: 0.9, cagr: 0.057, max_drawdown: -0.09, base: true },
      { params: { months: 12, assets: ["SPY", "EFA"] }, sharpe: 0.96, cagr: 0.06, max_drawdown: -0.08, base: false },
    ],
    base_sharpe: 0.9,
    worst_neighbor_sharpe: 0.96,
    passed: true,
  },
  monte_carlo: { paths: 1000, cagr_p5: 0.022, cagr_p50: 0.05, cagr_p95: 0.097, max_drawdown_p5: -0.156, prob_loss: 0.01 },
  regimes: { trend: [{ label: "up", days: 900, share: 0.6, ann_return: 0.057, ann_volatility: 0.08, sharpe: 0.7, benchmark_ann_return: 0.2 }] },
  test_touches: 2,
  gate: {
    passed: false,
    checks: [
      { name: "Locked test set", passed: true, detail: "Sharpe 1.06 after costs over 444 sessions" },
      { name: "Stress periods", passed: false, detail: "Aug 2024: -2.8% vs SPY -1.7%" },
    ],
  },
};

const data: Validation = {
  strategies: [
    { name: "ma-timing", title: report.title, spec: "", on_paper: true, report },
    { name: "rsi2", title: "RSI(2) short-term mean reversion", spec: "", on_paper: false, report: null },
  ],
  job: { running: false },
  periods: { train_end: "2023-06-30", validation_end: "2024-12-31" },
  gate: { min_test_sharpe: 0 },
};

afterEach(() => vi.unstubAllGlobals());

describe("ValidationView", () => {
  it("lists every strategy's gate status and opens a report", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(data)))));
    render(<ValidationView />);
    const row = await screen.findByRole("button", { name: report.title });
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(screen.getByText("not run")).toBeInTheDocument();
    expect(screen.getByText(/Paper trading 1 strategy that hasn't passed/)).toBeInTheDocument();
    fireEvent.click(row);
    expect(await screen.findByText("Not through the gate")).toBeInTheDocument();
    expect(screen.getByText(/Stress periods: fail/)).toBeInTheDocument();
    expect(screen.getByText("Aug 2024: -2.8% vs SPY -1.7%")).toBeInTheDocument();
    expect(screen.getByText(/looked at 2 times/)).toBeInTheDocument();
    expect(screen.getByText("months=12")).toBeInTheDocument();
    expect(screen.getByText("not in the backtest")).toBeInTheDocument();
  });

  it("starts a validation run", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(data))));
    vi.stubGlobal("fetch", fetchMock);
    render(<ValidationView />);
    fireEvent.click(await screen.findByRole("button", { name: "Validate all" }));
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true));
  });
});
