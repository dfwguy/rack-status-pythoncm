import { Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { RackSummary } from "../types/api";

interface StatusSummaryProps {
  summary: RackSummary;
  updatedAt: string;
  legend?: string;
}

export function StatusSummary({ summary, updatedAt, legend }: StatusSummaryProps) {
  const total = summary.done + summary.fail + summary.ongoing + summary.pending;

  return (
    <Card>
      <Flex gap="3" align="center" wrap="wrap">
        <Tag color="green" kind="solid" readOnly>{summary.done} done</Tag>
        <Tag color="red" kind="solid" readOnly>{summary.fail} fail</Tag>
        <Tag color="yellow" kind="solid" readOnly>{summary.ongoing} ongoing</Tag>
        <Tag color="blue" kind="solid" readOnly>{summary.pending} pending</Tag>
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
    </Card>
  );
}
