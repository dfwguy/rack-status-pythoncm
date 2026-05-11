import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { FooterNotes } from "./FooterNotes";

describe("FooterNotes", () => {
  it("renders footnotes", () => {
    render(<FooterNotes footnotes={["Note 1", "Note 2"]} />);
    expect(screen.getByText("Note 1")).toBeInTheDocument();
    expect(screen.getByText("Note 2")).toBeInTheDocument();
  });

  it("renders nothing when empty", () => {
    const { container } = render(<FooterNotes footnotes={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
