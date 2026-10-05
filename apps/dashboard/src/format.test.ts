import { issueSummary, money, pct, signedMoney, timeAgo } from "./format";

describe("format", () => {
  it("formats money with cents below $1,000", () => {
    expect(money(12.5)).toBe("$12.50");
    expect(money(61371.78)).toBe("$61,372");
    expect(money(null)).toBe("—");
  });

  it("signs deltas with a real minus", () => {
    expect(signedMoney(3.2)).toBe("+$3.20");
    expect(signedMoney(-1)).toBe("−$1.00");
    expect(pct(-0.0123, 2, true)).toBe("−1.23%");
    expect(pct(0.5, 0)).toBe("50%");
  });

  it("describes elapsed time", () => {
    const now = new Date("2026-10-05T12:00:00Z");
    expect(timeAgo("2026-10-05T11:59:30Z", now)).toBe("30s ago");
    expect(timeAgo("2026-10-05T09:00:00Z", now)).toBe("3h ago");
    expect(timeAgo(null, now)).toBe("never");
  });

  it("summarises validation counts", () => {
    expect(issueSummary({})).toBe("no issues");
    expect(issueSummary({ "warning:zero_volume": 3, "error:ohlc": 1 })).toBe("3 warning: zero volume, 1 error: ohlc");
  });
});
