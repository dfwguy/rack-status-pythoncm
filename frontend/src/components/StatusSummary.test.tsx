import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusSummary } from "./StatusSummary";

describe("StatusSummary", () => {
  const defaultProps = {
    summary: { done: 10, fail: 2, ongoing: 3, pending: 3 },
    updatedAt: new Date().toISOString(),
  };

  it("renders rack count", () => {
    render(<StatusSummary {...defaultProps} />);
    expect(screen.getByText("18 racks total")).toBeInTheDocument();
  });

  it("renders all status tags", () => {
    render(<StatusSummary {...defaultProps} />);
    expect(screen.getByText("10 done")).toBeInTheDocument();
    expect(screen.getByText("2 fail")).toBeInTheDocument();
    expect(screen.getByText("3 ongoing")).toBeInTheDocument();
    expect(screen.getByText("3 pending")).toBeInTheDocument();
  });

  it("renders legend when provided", () => {
    render(<StatusSummary {...defaultProps} legend="Test legend text" />);
    expect(screen.getByText("Test legend text")).toBeInTheDocument();
  });

  it("does not render legend when absent", () => {
    render(<StatusSummary {...defaultProps} />);
    expect(screen.queryByText("Test legend text")).not.toBeInTheDocument();
  });
});
