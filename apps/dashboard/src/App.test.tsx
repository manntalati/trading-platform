import { parseRoute } from "./App";

describe("parseRoute", () => {
  it("reads section, view and detail", () => {
    expect(parseRoute("#/portfolio/positions/XYZ260116C00100000")).toEqual({
      section: "portfolio",
      sub: "positions",
      detail: "XYZ260116C00100000",
    });
    expect(parseRoute("#/research")).toEqual({ section: "research", sub: null, detail: null });
    expect(parseRoute("")).toEqual({ section: "home", sub: null, detail: null });
    expect(parseRoute("#/nowhere/x")).toEqual({ section: "home", sub: null, detail: null });
  });

  it("sends old addresses to where things live now", () => {
    expect(parseRoute("#/overview").section).toBe("home");
    expect(parseRoute("#/positions/XYZ")).toEqual({ section: "portfolio", sub: "positions", detail: "XYZ" });
    expect(parseRoute("#/ideas")).toEqual({ section: "research", sub: "checks", detail: null });
    expect(parseRoute("#/options")).toEqual({ section: "research", sub: "lookup", detail: null });
    expect(parseRoute("#/strategies")).toEqual({ section: "paper", sub: "strategies", detail: null });
  });
});
