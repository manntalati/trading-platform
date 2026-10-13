import type { OptionPositionRow, Positions, Status } from "../types";
import { attentionItems } from "./Home";

const option = {
  symbol: "XYZ260116C00100000",
  label: "XYZ Jan 16 '26 $100 Call",
  kind: "option",
  notes: [{ level: "risk", title: "Exercise at expiry", text: "You'd buy 100 shares." }],
  actions: [{ level: "consider", title: "Take profits", text: "Up 60%.", trade: null }],
  stats: { days_left: 3 },
} as unknown as OptionPositionRow;

describe("attentionItems", () => {
  it("puts exercise risk first, then actions, approvals and stale data", () => {
    const positions = { synced: true, positions: [option] } as unknown as Positions;
    const status = { bars: { stale: true, latest_session: "2026-01-08", expected_session: "2026-01-09" } } as unknown as Status;
    const items = attentionItems(positions, status, 2);
    expect(items.map((i) => i.title)).toEqual([
      "XYZ Jan 16 '26 $100 Call: Exercise at expiry",
      "XYZ Jan 16 '26 $100 Call: Take profits",
      "2 paper orders waiting for your approval",
      "Daily prices are behind",
    ]);
    expect(items[0]?.href).toBe("#/portfolio/positions/XYZ260116C00100000");
    expect(items[2]?.href).toBe("#/paper");
  });

  it("is empty when nothing needs you", () => {
    expect(attentionItems(null, null, 0)).toEqual([]);
  });
});
