import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ErrorBanner } from "./ErrorBanner";

describe("ErrorBanner", () => {
  it("renders error message", () => {
    render(<ErrorBanner message="Something went wrong" />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });
});
