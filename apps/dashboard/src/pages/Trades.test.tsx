import { render, screen } from "@testing-library/react";
import type { Trade } from "../types";
import { TradeTable } from "./Trades";

const base: Trade = {
  date: "2026-01-09",
  source: "broker",
  account: "Fidelity (demo) …1234",
  strategy: null,
  symbol: "XYZ260116C00050000",
  label: "XYZ Jan 16 '26 $50 Call",
  kind: "option",
  action: "Sell to close",
  quantity: 1,
  price: 4.5,
  amount: 449.33,
  fee: 0.67,
  realized_pnl: 148.68,
  cost_known: true,
  pending: false,
};

describe("TradeTable", () => {
  it("shows your trades and the paper bot's, with what each realized", () => {
    const rows: Trade[] = [
      base,
      { ...base, symbol: "OLD", label: "OLD", kind: "equity", action: "Sell", realized_pnl: null, cost_known: false },
      { ...base, source: "paper", account: "Paper · ma-timing", strategy: "ma-timing", symbol: "SPY", label: "SPY", kind: "equity", action: "Buy", realized_pnl: null },
    ];
    rows.push({ ...base, symbol: "NEW", label: "NEW", kind: "equity", action: "Bought", realized_pnl: null, pending: true });
    render(<TradeTable rows={rows} />);
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByText("XYZ Jan 16 '26 $50 Call")).toBeInTheDocument();
    expect(screen.getByText(/\+\$148\.68/)).toBeInTheDocument();
    expect(screen.getByText("cost unknown")).toBeInTheDocument();
    expect(screen.getByText("Paper")).toBeInTheDocument();
    expect(screen.getByText(/ma-timing/)).toBeInTheDocument();
  });
});
