import Box from "@mui/material/Box";
import { Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { RackSummary } from "../types/api";

export type ValidationSummaryCategory = "done" | "fail" | "ongoing" | "pending";

interface StatusSummaryProps {
  summary: RackSummary;
  updatedAt: string;
  legend?: string;
  /** Active validation filters (same strings as the rack table). When used with ``onToggleValidationCategory``, summary tags toggle filters. */
  validationFilters?: string[];
  onToggleValidationCategory?: (category: ValidationSummaryCategory) => void;
}

function validationTagColor(state: ValidationSummaryCategory): "green" | "red" | "yellow" | "blue" {
  switch (state) {
    case "done":
      return "green";
    case "fail":
      return "red";
    case "ongoing":
      return "yellow";
    case "pending":
      return "blue";
  }
}

export function StatusSummary({
  summary,
  updatedAt,
  legend,
  validationFilters = [],
  onToggleValidationCategory,
}: StatusSummaryProps) {
  const total = summary.done + summary.fail + summary.ongoing + summary.pending;
  const interactive = Boolean(onToggleValidationCategory);

  const chip = (category: ValidationSummaryCategory, count: number, label: string) => {
    const active = validationFilters.includes(category);
    const tag = (
      <Tag color={validationTagColor(category)} kind="solid" readOnly density="compact">
        {count} {label}
      </Tag>
    );
    if (!interactive) {
      return <span key={category}>{tag}</span>;
    }
    return (
      <Box
        key={category}
        component="button"
        type="button"
        onClick={() => onToggleValidationCategory?.(category)}
        aria-pressed={active}
        title={
          active
            ? `${label}: remove from filter (show more racks)`
            : `${label}: add to validation filter`
        }
        sx={{
          border: "none",
          background: "transparent",
          padding: 0,
          margin: 0,
          cursor: "pointer",
          borderRadius: 9999,
          display: "inline-flex",
          boxShadow: (theme) => (active ? `0 0 0 2px ${theme.palette.primary.main}` : "none"),
          "&:focus-visible": {
            outline: `2px solid`,
            outlineOffset: 2,
          },
        }}
      >
        {tag}
      </Box>
    );
  };

  return (
    <Card>
      <Flex gap="3" align="center" wrap="wrap">
        {chip("done", summary.done, "done")}
        {chip("fail", summary.fail, "fail")}
        {chip("ongoing", summary.ongoing, "ongoing")}
        {chip("pending", summary.pending, "pending")}
        <Text kind="body/semibold/md" style={{ color: "var(--text-color-secondary)" }}>
          {total} racks total
        </Text>
        <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)", marginLeft: "auto" }}>
          Updated: {new Date(updatedAt).toLocaleTimeString()}
        </Text>
      </Flex>
      {legend && (
        <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}>
          {legend}
        </Text>
      )}
      {interactive && validationFilters.length > 0 && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}>
          Filtering table by: {validationFilters.join(", ")} — click a highlighted tag again to remove it, or use Clear filters
          in the table.
        </Text>
      )}
    </Card>
  );
}
