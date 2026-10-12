import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { vi } from "vitest";
import type { ChatTurn, OptionPositionRow, Positions as PositionsData } from "../types";
import Positions, { AskPanel, CashNote, Scenarios } from "./Positions";

const call: OptionPositionRow = {
  symbol: "XYZ260116C00100000",
  label: "XYZ Jan 16 '26 $100 Call",
  description: null,
  kind: "option",
  underlying: "XYZ",
  quantity: 2,
  price: 1.05,
  price_source: "mid",
  broker_price: 1.2,
  cost: 2.5,
  market_value: 210,
  pnl: -290,
  pnl_pct: -0.58,
  day_pnl: null,
  weight: 0.02,
  sector: "Options",
  asset_class: "Options",
  notes: [
    { level: "warn", title: "Time decay", text: "Losing about $48 a day to time alone." },
    { level: "risk", title: "Exercise at expiry", text: "You'd buy 200 shares." },
  ],
  stats: {
    right: "call",
    strike: 100,
    expiration: "2026-01-16",
    expires_at: "2026-01-16T21:00:00+00:00",
    days_left: 4.25,
    spot: 97,
    mark: 1.05,
    mark_source: "mid",
    bid: 1,
    ask: 1.1,
    quote_at: null,
    cost: 2.5,
    value: 210,
    pnl: -290,
    exit_value: 200,
    breakeven: 102.5,
    breakeven_move_pct: 0.0567,
    moneyness: -0.03,
    in_the_money: false,
    iv: 0.42,
    theta_day: -48.2,
    prob_profit: 0.12,
    prob_itm: 0.24,
    scenarios: {
      columns: ["Now", "Expiry Fri Jan 16"],
      rows: [
        { move: 0, price: 97, cells: [{ value: 210, pnl: -290 }, { value: 0, pnl: -500 }] },
        { move: 0.05, price: 101.85, cells: [{ value: 450, pnl: -50 }, { value: 370, pnl: -130 }] },
      ],
    },
  },
  underlying_stats: { price: 97 },
};

const view: PositionsData = {
  synced: true,
  total_value: 10_000,
  cash: { cash: 600, reported_cash: 1000, unsettled: 400, buying_power: 600, holdings_value: 9400, total_value: 10_000 },
  market_data: { source: "alpaca", error: null, option_quotes: 1, options: 1 },
  totals: { value: 9400, pnl: -290, day_pnl: 12, options_value: 210, options_pnl: -290, theta_day: -48.2, expiring_within_7d: 1 },
  exposure: [],
  positions: [call],
  ask: { available: true, model: "claude-opus-5-5" },
};

afterEach(() => vi.unstubAllGlobals());

describe("CashNote", () => {
  it("explains cash that's paying for unsettled trades", () => {
    render(<CashNote cash={view.cash!} />);
    expect(screen.getByText(/Cash is \$600\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\$400\.00 of it is paying for trades that haven't settled/)).toBeInTheDocument();
  });

  it("stays quiet when cash adds up", () => {
    const { container } = render(<CashNote cash={{ ...view.cash!, unsettled: 0.2 }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("Scenarios", () => {
  it("shows the P&L for each move and date", () => {
    render(<Scenarios grid={call.stats.scenarios!} cost={2.5} quantity={2} />);
    expect(screen.getByText("Expiry Fri Jan 16")).toBeInTheDocument();
    expect(screen.getByText("unchanged")).toBeInTheDocument();
    expect(screen.getByText(/−\$500\.00/)).toBeInTheDocument();
    expect(screen.getByText(/against \$500\.00 paid/)).toBeInTheDocument();
  });
});

describe("Positions page", () => {
  it("lists options with their odds and opens one with its notes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(view)))));
    function Harness() {
      const [selected, setSelected] = useState<string | null>(null);
      return <Positions selected={selected} onSelect={setSelected} />;
    }
    render(<Harness />);
    const link = await screen.findByRole("button", { name: "XYZ Jan 16 '26 $100 Call" });
    expect(screen.getByText("action needed")).toBeInTheDocument();
    expect(screen.getByText("12%")).toBeInTheDocument(); // odds of profit
    fireEvent.click(link);
    expect(await screen.findByText("Losing about $48 a day to time alone.")).toBeInTheDocument();
    expect(screen.getByText("Should I hold this to expiry or sell now?")).toBeInTheDocument();
    expect(screen.getByText("Breakeven at expiry")).toBeInTheDocument();
  });
});

describe("AskPanel", () => {
  it("sends the question with the conversation so far and shows the answer", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          answer: "Sell before Thursday's earnings.",
          model: "claude-opus-5-5",
          stop_reason: "end_turn",
          sources: [{ url: "https://example.test/e", title: "Earnings calendar" }],
          searches: 1,
          usage: { input_tokens: 2000, output_tokens: 300 },
          note: null,
          as_of: "2026-01-12T15:00:00Z",
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    function Harness() {
      const [thread, setThread] = useState<ChatTurn[]>([]);
      return <AskPanel symbol={call.symbol} title="Ask" ask={view.ask} thread={thread} onThread={setThread} suggestions={["Hold or sell?"]} />;
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Hold or sell?" }));
    expect(await screen.findByText("Sell before Thursday's earnings.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Earnings calendar" })).toHaveAttribute("rel", "noreferrer noopener");
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/positions/ask");
    expect(JSON.parse(init.body as string)).toEqual({ question: "Hold or sell?", symbol: call.symbol, history: [] });

    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "What if it gaps up?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const second = JSON.parse((fetchMock.mock.calls[1] as [string, RequestInit])[1].body as string);
    expect(second.history).toEqual([
      { role: "user", content: "Hold or sell?" },
      { role: "assistant", content: "Sell before Thursday's earnings." },
    ]);
  });

  it("says how to turn it on without a key", () => {
    render(<AskPanel symbol={null} title="Ask" ask={{ available: false, model: "m" }} thread={[]} onThread={vi.fn()} suggestions={[]} />);
    expect(screen.getByText(/ANTHROPIC_API_KEY/)).toBeInTheDocument();
  });
});
