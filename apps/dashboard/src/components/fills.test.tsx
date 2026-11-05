import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { BudgetLine, instrumentOf } from "../pages/Playbook";
import type { FillsView } from "../types";
import { RecordedTrades } from "./fills";

afterEach(() => vi.unstubAllGlobals());

const sold = {
  id: 1,
  at: new Date().toISOString(),
  account: "Fidelity …1234",
  legs: [{ symbol: "XYZ", quantity: -40, price: 52, kind: "stock" as const, label: "XYZ shares", unit: "shares" as const, underlying: "XYZ" }],
  cash: 2080,
  settles: "2026-11-05",
  note: null,
  journal_id: null,
  status: "pending" as const,
  counts: true,
};

function stub(views: FillsView[]) {
  const calls: { method: string; path: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((path: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const view = views[Math.min(calls.length - 1, views.length - 1)];
      return Promise.resolve(new Response(JSON.stringify(view)));
    }),
  );
  return calls;
}

describe("RecordedTrades", () => {
  const accounts = [
    { key: "1234", label: "Fidelity …1234" },
    { key: "5678", label: "Fidelity …5678" },
  ];
  const holdings = [
    { symbol: "XYZ", quantity: 40, price: 50, account: "1234", kind: "stock" as const, label: "XYZ shares", unit: "shares" as const, underlying: "XYZ" },
    {
      symbol: "XYZ261218C00025000",
      quantity: 2,
      price: 1.2,
      account: "5678",
      kind: "call" as const,
      label: "XYZ Dec 18 '26 $25 Call",
      unit: "contracts" as const,
      underlying: "XYZ",
      expiration: "2026-12-18",
      strike: 25,
    },
  ];

  it("records a stock sale and lists it with what it is", async () => {
    const calls = stub([
      { fills: [], accounts, holdings, synced: true },
      { fills: [sold], accounts, holdings, synced: true },
    ]);
    const changed = vi.fn();
    render(<RecordedTrades onChange={changed} />);
    await waitFor(() => expect(screen.getByLabelText("Account")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: "xyz" } });
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "40" } });
    fireEvent.change(screen.getByLabelText("Price"), { target: { value: "52" } });
    expect(screen.getByText("Sold 40 shares of XYZ at $52.00 a share: $2,080.00 received.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const post = calls.find((c) => c.method === "POST");
    expect(post?.path).toBe("/api/fills");
    expect(post?.body).toEqual({ side: "sell", kind: "stock", symbol: "xyz", expiration: null, strike: null, quantity: 40, price: 52, account: null });
    const row = (await screen.findByText("XYZ")).closest("li")!;
    expect(row).toHaveTextContent("Stock sold 40 shares of XYZ at $52.00");
    expect(screen.getByText("counted")).toHaveAttribute("title", expect.stringMatching(/doesn't show it yet/));
    expect(screen.getByText("(+$2,080.00)")).toBeInTheDocument();
  });

  it("takes a call by its expiration and strike, and says what a contract costs", async () => {
    const calls = stub([{ fills: [], accounts, holdings, synced: true }]);
    render(<RecordedTrades />);
    await waitFor(() => expect(screen.getByLabelText("Account")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Bought" }));
    expect(screen.queryByLabelText("Expiration")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Call" }));
    expect(screen.getByText("Contracts")).toBeInTheDocument();
    expect(screen.getByText("Underlying")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: "abc" } });
    fireEvent.change(screen.getByLabelText("Expiration"), { target: { value: "2026-12-18" } });
    fireEvent.change(screen.getByLabelText("Strike"), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Price"), { target: { value: "1.45" } });
    expect(screen.getByText("Bought 2 contracts of ABC Dec 18 '26 $25 Call at $1.45 a share ($145.00 a contract): $290.00 paid.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ side: "buy", kind: "call", symbol: "abc", expiration: "2026-12-18", strike: 25, quantity: 2 });
  });

  it("fills a sale in from what you hold", async () => {
    stub([{ fills: [], accounts, holdings, synced: true }]);
    render(<RecordedTrades />);
    const from = await screen.findByLabelText("From what you hold");
    expect(screen.getByRole("option", { name: "Call: XYZ Dec 18 '26 $25 Call (2 contracts)" })).toBeInTheDocument();
    fireEvent.change(from, { target: { value: "5678|XYZ261218C00025000" } });
    expect(screen.getByLabelText("Strike")).toHaveValue("25");
    expect(screen.getByLabelText("Expiration")).toHaveValue("2026-12-18");
    expect(screen.getByLabelText("Quantity")).toHaveValue("2");
    expect(screen.getByLabelText("Account")).toHaveValue("5678");
    expect(screen.getByRole("button", { name: "Call" })).toHaveAttribute("aria-pressed", "true");
  });

  it("asks for a sync first when there's no account to count against", async () => {
    stub([{ fills: [], accounts: [], synced: false }]);
    render(<RecordedTrades />);
    expect(await screen.findByText(/Sync the brokerage once first/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();
  });
});

describe("instrumentOf", () => {
  it("names an idea's instrument", () => {
    expect(instrumentOf({ kind: "shares" })).toEqual({ kind: "stock", label: "Stock" });
    const play = { legs: [], model: {}, structure: "call debit spread" };
    expect(instrumentOf(play)).toEqual({ kind: "call", label: "Call spread" });
    expect(instrumentOf({ ...play, structure: "cash-secured put" })).toEqual({ kind: "put", label: "Cash-secured put" });
    expect(instrumentOf({ ...play, structure: "LEAPS call" })).toEqual({ kind: "call", label: "LEAPS call" });
    expect(instrumentOf({ ...play, structure: "iron condor" })).toEqual({ kind: "option", label: "Iron condor" });
    expect(instrumentOf(null)).toBeNull();
  });
});

describe("BudgetLine with recorded trades", () => {
  it("says how much of the buying power comes from trades the sync doesn't show", () => {
    render(
      <BudgetLine
        budget={{
          buying_power: 5080,
          account_value: 20000,
          account: "Fidelity …1234",
          risk_per_trade: 0.02,
          risk_cap: 400,
          options_level: null,
          as_of: null,
          paper: false,
          pending: 2080,
          pending_fills: 1,
        }}
      />,
    );
    expect(screen.getByText("$2,080")).toBeInTheDocument();
    expect(screen.getByText(/including/)).toHaveTextContent(/from 1 trade you recorded that Fidelity's numbers don't show yet/);
  });
});
