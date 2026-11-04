import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { BudgetLine } from "../pages/Playbook";
import type { FillsView } from "../types";
import { RecordedTrades } from "./fills";

afterEach(() => vi.unstubAllGlobals());

const sold = {
  id: 1,
  at: new Date().toISOString(),
  account: "Fidelity …1234",
  legs: [{ symbol: "XYZ", quantity: -40, price: 52 }],
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
  it("records a sale and lists what's counted until the sync shows it", async () => {
    const accounts = [
      { key: "1234", label: "Fidelity …1234" },
      { key: "5678", label: "Fidelity …5678" },
    ];
    const calls = stub([
      { fills: [], accounts, synced: true },
      { fills: [sold], accounts, synced: true },
    ]);
    const changed = vi.fn();
    render(<RecordedTrades onChange={changed} />);
    await waitFor(() => expect(screen.getByLabelText("Account")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: "xyz" } });
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "40" } });
    fireEvent.change(screen.getByLabelText("Price"), { target: { value: "52" } });
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const post = calls.find((c) => c.method === "POST");
    expect(post?.path).toBe("/api/fills");
    expect(post?.body).toEqual({ side: "sell", symbol: "xyz", quantity: 40, price: 52, account: null });
    expect(await screen.findByText(/sold 40 XYZ at \$52\.00/)).toBeInTheDocument();
    expect(screen.getByText("counted")).toHaveAttribute("title", expect.stringMatching(/doesn't show it yet/));
    expect(screen.getByText("(+$2,080.00)")).toBeInTheDocument();
  });

  it("asks for a sync first when there's no account to count against", async () => {
    stub([{ fills: [], accounts: [], synced: false }]);
    render(<RecordedTrades />);
    expect(await screen.findByText(/Sync the brokerage once first/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();
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
