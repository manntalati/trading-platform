import { render, screen } from "@testing-library/react";
import type { Trade } from "../types";
import { OptionsCard, breakevenNote, type OptionRow } from "./Portfolio";
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

describe("OptionsCard", () => {
  it("quotes premiums per share and flags contracts about to expire", () => {
    const option = {
      symbol: "XYZ260116C00050000",
      label: "XYZ Jan 16 '26 $50 Call",
      description: null,
      kind: "option",
      underlying: "XYZ",
      quantity: 2,
      price: 450,
      market_value: 900,
      cost_basis_per_unit: 300,
      unrealized_pnl: 300,
      unrealized_pct: 0.5,
      weight: 0.1,
      sector: "Information Technology",
      asset_class: "Options",
      priced: false,
      multiplier: 100,
      expiration: "2026-01-16",
      strike: 50,
      right: "C",
      days_to_expiry: 3,
      premium: 4.5,
      cost_premium: 3,
      underlying_price: 52,
      breakeven: 53,
      moneyness: 0.04,
      spot: 52,
    } satisfies OptionRow;
    render(<OptionsCard options={[option]} />);
    expect(screen.getByText("$4.50")).toBeInTheDocument(); // premium per share
    expect(screen.getByText("$900.00")).toBeInTheDocument(); // 2 contracts
    expect(screen.getByText(/in the money by 4\.0%/)).toBeInTheDocument();
    expect(screen.getByText("in 3 days")).toBeInTheDocument();
  });
});

describe("breakevenNote", () => {
  it("says which way the underlying has to move", () => {
    expect(breakevenNote({ right: "C", spot: 50, breakeven: 55 })).toBe("needs +10.0%");
    expect(breakevenNote({ right: "C", spot: 60, breakeven: 55 })).toBe("above breakeven");
    expect(breakevenNote({ right: "P", spot: 50, breakeven: 45 })).toBe("needs −10.0%");
    expect(breakevenNote({ right: "P", spot: 40, breakeven: 45 })).toBe("below breakeven");
  });
});
