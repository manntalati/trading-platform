import { render, screen } from "@testing-library/react";
import { FidelityFreshness } from "./broker";

const fresh = {
  synced_at: "2026-01-09T15:30:00Z",
  positions_as_of: "2026-01-09T14:42:00Z",
  transactions_through: "2026-01-08",
};

describe("FidelityFreshness", () => {
  it("says how current the data is, with no warning when it's healthy", () => {
    render(<FidelityFreshness freshness={{ ...fresh, problems: [] }} />);
    expect(screen.getByText(/transactions through 2026-01-08/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("warns when the connection has stopped updating, and says what to run", () => {
    const problem =
      "Fidelity …1234: SnapTrade's connection to the broker is disabled, so it keeps serving the last data it got and new trades won't arrive.";
    render(<FidelityFreshness freshness={{ ...fresh, problems: [problem] }} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Fidelity data isn't updating");
    expect(alert).toHaveTextContent("disabled");
    expect(alert).toHaveTextContent("tp-broker check");
  });
});
