import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { postJson } from "../api";
import type { PaperProposal } from "../types";
import { ProposalRow } from "./Paper";

const proposal: PaperProposal = {
  id: "ma-timing-20240731-SPY-buy-1",
  strategy: "ma-timing",
  session: "2024-07-31",
  symbol: "SPY",
  side: "buy",
  quantity: 9,
  approved_quantity: null,
  order_quantity: 9,
  notional: 4_500,
  reference_price: 500,
  order_type: "market",
  limit_price: null,
  reason: "SPY month-end close 500.00 is above its 10-month average 480.00: invested",
  checks: [
    { check: "kill_switch", passed: true, detail: "off" },
    { check: "gross_exposure", passed: true, detail: "4.5% of equity after this order (limit 100%)" },
  ],
  status: "pending",
  note: "",
  decided_by: null,
  decided_at: null,
  filled_quantity: 0,
  avg_fill_price: null,
  created_at: "2024-07-31T23:00:00+00:00",
  updated_at: "2024-07-31T23:00:00+00:00",
};

describe("ProposalRow", () => {
  it("shows the order, the strategy's reason and the risk checks on demand", () => {
    render(<ProposalRow p={proposal} busy={false} onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText("▲ Buy")).toBeInTheDocument();
    expect(screen.getByText(/month-end close 500.00/)).toBeInTheDocument();
    expect(screen.queryByText("gross_exposure")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /2 risk checks passed/ }));
    expect(screen.getByText("gross_exposure")).toBeInTheDocument();
  });

  it("approves as proposed, or fewer shares, and never an invalid amount", () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<ProposalRow p={proposal} busy={false} onApprove={onApprove} onReject={onReject} />);
    const approve = screen.getByRole("button", { name: "Approve" });
    fireEvent.click(approve);
    expect(onApprove).toHaveBeenLastCalledWith(proposal, null);

    const shares = screen.getByLabelText("Shares of SPY to buy");
    fireEvent.change(shares, { target: { value: "4" } });
    fireEvent.click(approve);
    expect(onApprove).toHaveBeenLastCalledWith(proposal, 4);

    fireEvent.change(shares, { target: { value: "12" } });
    expect(approve).toBeDisabled();
    expect(screen.getByText("1 to 9 whole shares")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onReject).toHaveBeenCalledWith(proposal);
  });
});

describe("postJson", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the dashboard header and surfaces the API's error detail", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "x is rejected, not pending" }), { status: 409 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(postJson("/api/paper/proposals/x/approve", {})).rejects.toThrow("x is rejected, not pending");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-TP-Client"]).toBe("dashboard");
  });
});
