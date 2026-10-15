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

describe("SectionMenu", () => {
  it("lists every section and jumps with number keys", async () => {
    const { render, screen, fireEvent } = await import("@testing-library/react");
    const { SectionMenu } = await import("./App");
    window.location.hash = "#/home";
    render(<SectionMenu current="home" pending={2} />);
    fireEvent.click(screen.getByRole("button", { name: /Home/ }));
    const items = screen.getAllByRole("menuitem");
    expect(items).toHaveLength(6);
    expect(items[3]).toHaveTextContent("Trades");
    expect(items[4]).toHaveTextContent("2"); // approvals waiting, on Paper trading
    fireEvent.keyDown(window, { key: "4" });
    expect(window.location.hash).toBe("#/trades");
  });
});
