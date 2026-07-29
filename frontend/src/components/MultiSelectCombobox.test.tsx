import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MultiSelectCombobox } from "./MultiSelectCombobox";

const ITEMS = [
  { value: "a", children: "Alpha" },
  { value: "b", children: "Bravo" },
  { value: "c", children: "Charlie" },
];

describe("MultiSelectCombobox", () => {
  it("shows the default singular/plural count summary", () => {
    const { rerender } = render(
      <MultiSelectCombobox items={ITEMS} value={["a"]} onValueChange={vi.fn()} aria-label="Filter" />,
    );
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    rerender(
      <MultiSelectCombobox items={ITEMS} value={["a", "b"]} onValueChange={vi.fn()} aria-label="Filter" />,
    );
    expect(screen.getByText("2 selected")).toBeInTheDocument();
  });

  it("renders a compact count summary in the trigger (count mode, not per-selection tags)", () => {
    // The trigger shows a single fixed-size "<n> selected" summary rather than
    // one growing tag per selection — this is the layout-overflow fix. (Option
    // labels still exist in the listbox markup; that is expected.)
    render(
      <MultiSelectCombobox
        items={ITEMS}
        value={["a", "b", "c"]}
        onValueChange={vi.fn()}
        aria-label="Filter"
      />,
    );
    expect(screen.getByText("3 selected")).toBeInTheDocument();
  });

  it("honors a custom summary formatter", () => {
    render(
      <MultiSelectCombobox
        items={ITEMS}
        value={["a", "b"]}
        onValueChange={vi.fn()}
        aria-label="Filter"
        formatSummaryLabel={(n) => `${n} racks`}
      />,
    );
    expect(screen.getByText("2 racks")).toBeInTheDocument();
  });
});
