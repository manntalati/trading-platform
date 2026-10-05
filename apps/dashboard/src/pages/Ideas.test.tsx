import { render, screen } from "@testing-library/react";
import { IdeaCard } from "./Ideas";

describe("IdeaCard", () => {
  it("shows severity as text, not just color, and every rationale line", () => {
    render(
      <IdeaCard
        idea={{
          kind: "portfolio",
          severity: "attention",
          title: "NVDA is 30% of the portfolio",
          summary: "Above the 10% single-position cap.",
          rationale: ["Market value 18,661"],
          symbols: ["NVDA"],
          score: 0.3,
          metrics: {},
        }}
      />,
    );
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "NVDA is 30% of the portfolio" })).toBeInTheDocument();
    expect(screen.getByText("Market value 18,661")).toBeInTheDocument();
  });
});
