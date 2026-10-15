import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { Rail } from "./rail";

const responses: Record<string, unknown> = {
  "/api/portfolio": {
    synced: true,
    total_value: 25000,
    cash: 1200,
    accounts: [{ source: "fake", account_name: "Individual", account_number_masked: "…1234", institution: "Fidelity (demo)", cash: 1200, total_value: 25000, taken_at: "2026-01-09T15:30:00Z" }],
    holdings: [],
    freshness: { synced_at: "2026-01-09T15:30:00Z", positions_as_of: null, transactions_through: "2026-01-08", problems: [] },
  },
  "/api/trades/stats": {
    synced: true,
    by_kind: {
      all: { trades: 12, win_rate: 0.58, median_return: 0.12, total_pnl: 840 },
      calls: { trades: 8, win_rate: 0.5, median_return: 0.2, median_days: 9 },
      puts: { trades: 0 },
      shares: { trades: 4, win_rate: 0.75, median_return: 0.08, median_days: 64 },
      options_sold: { trades: 0 },
    },
  },
  "/api/market/regime": { current: null, history: [] },
};

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((path: string) => {
      const body = responses[path.split("?")[0] ?? ""];
      return Promise.resolve(body ? new Response(JSON.stringify(body)) : new Response("{}", { status: 404 }));
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("Rail", () => {
  it("summarises the account and your trading, each linking to its page", async () => {
    render(<Rail />);
    expect((await screen.findAllByText("$25,000")).length).toBeGreaterThan(0); // header and account row
    expect(await screen.findByText("58%")).toBeInTheDocument(); // win rate in the panel's header
    expect(screen.getByText("Calls · 8")).toBeInTheDocument();
    expect(screen.getByText("+20% · held 9d")).toBeInTheDocument();
    expect(screen.getByText("Shares · 4")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Your trading" })).toHaveAttribute("href", "#/trades/stats");
    expect(screen.getByRole("link", { name: "Open Account" })).toHaveAttribute("href", "#/portfolio");
  });

  it("says it is loading, or that a request failed, instead of claiming there is nothing", async () => {
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((path: string) =>
        path.startsWith("/api/portfolio")
          ? new Promise<Response>((resolve) => (answer = resolve))
          : Promise.resolve(new Response("boom", { status: 500 })),
      ),
    );
    render(<Rail />);
    const account = screen.getByRole("region", { name: "Account" });
    expect(account).toHaveTextContent("Loading…");
    expect(account).not.toHaveTextContent("not synced");
    expect(await screen.findByText(/Couldn't load/)).toBeInTheDocument(); // the trades stats request failed
    answer(new Response(JSON.stringify(responses["/api/portfolio"])));
    expect((await screen.findAllByText("$25,000")).length).toBeGreaterThan(0);
  });

  it("collapses and expands a panel, and remembers it", async () => {
    const { unmount } = render(<Rail />);
    await screen.findByText("Calls · 8");
    const toggle = screen.getByRole("button", { name: /Your trading/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Calls · 8")).not.toBeInTheDocument();
    unmount();
    render(<Rail />);
    expect(screen.getByRole("button", { name: /Your trading/ })).toHaveAttribute("aria-expanded", "false");
    // closed panels don't fetch what only their body shows
    expect(screen.getByRole("button", { name: /Positions/ })).toHaveAttribute("aria-expanded", "false");
  });
});
