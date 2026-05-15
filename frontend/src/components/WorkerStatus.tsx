import { Flex, Text } from "@nvidia/foundations-react-core";
import type { NativeWorkers } from "../types/api";

interface WorkerStatusProps {
  workers: NativeWorkers;
}

export function WorkerStatusPanel({ workers }: WorkerStatusProps) {
  return (
    <Flex gap="3" align="center" wrap="wrap" style={{ padding: "8px 4px" }}>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
        Backend: <strong>{workers.mode}</strong>
      </Text>
      {workers.rack_count != null && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {workers.rack_count} racks
        </Text>
      )}
      {workers.note && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", fontStyle: "italic" }}>
          {workers.note}
        </Text>
      )}
    </Flex>
  );
}
