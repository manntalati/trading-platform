import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { PortfolioRisk } from "../types";
import RiskView from "./Risk";

const risk: PortfolioRisk = {
  synced: true,
  coverage: 0.9,
  value: 100_000,
  sessions: 252,
  beta: 1.2,
  beta_dollars: 120_000,
  volatility: 0.18,
  var: { historical_95: 1800, historical_99: 3200, parametric_95: 1900, parametric_99: 2700, expected_shortfall_95: 2600 },
  worst_days: [{ date: "2025-04-04", pnl: -5100 }],
  correlation: { symbols: ["XYZ", "ABC"], matrix: [[1, 0.8], [0.8, 1]], average: 0.8, most_correlated: [{ a: "XYZ", b: "ABC", correlation: 0.8 }] },
  greeks: { delta_dollars: 15_000, gamma_1pct: 400, theta_day: -60, vega_point: 220 },
  scenarios: { vol_shifts: [-0.05, 0, 0.05], rows: [{ move: -0.1, pnl: [-14000, -13000, -12500] }, { move: 0, pnl: [-300, 0, 300] }] },
};

afterEach(() => vi.unstubAllGlobals());

describe("RiskView", () => {
  it("shows beta, VaR, the scenario grid and correlation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(risk))));
    render(<RiskView />);
    expect(await screen.findByText("1.20")).toBeInTheDocument();
    expect(screen.getByText(/per 1% S&P move/)).toBeInTheDocument();
    expect(screen.getAllByText("$1,800").length).toBeGreaterThan(0);
    expect(screen.getByText(/cover 90% of your exposure/)).toBeInTheDocument();
    expect(screen.getByText("IV +5 pts")).toBeInTheDocument();
    expect(screen.getByText(/XYZ\/ABC 0.80/)).toBeInTheDocument();
    expect(screen.getByText("−$60.00/day")).toBeInTheDocument();
  });
});
