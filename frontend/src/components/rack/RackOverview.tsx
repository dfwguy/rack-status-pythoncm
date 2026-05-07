import { Card, Flex, Tag, Text, Divider } from "@nvidia/foundations-react-core";
import type { RackRow } from "../../types/api";

interface RackOverviewProps {
  row: RackRow;
  comment: string;
}

function ValidationTag({ nemo }: { nemo: string }) {
  if (nemo === "done") return <Tag color="green" kind="solid" readOnly density="compact">done</Tag>;
  if (nemo === "fail") return <Tag color="red" kind="solid" readOnly density="compact">fail</Tag>;
  if (nemo.startsWith("ongoing")) return <Tag color="yellow" kind="solid" readOnly density="compact">{nemo}</Tag>;
  if (nemo === "pending") return <Tag color="blue" kind="solid" readOnly density="compact">pending</Tag>;
  return <Tag color="gray" kind="solid" readOnly density="compact">{nemo}</Tag>;
}

function InfoItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ minWidth: 120 }}>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>
        {label}
      </Text>
      <div style={{ marginTop: 4 }}>{children}</div>
    </div>
  );
}

export function RackOverview({ row, comment }: RackOverviewProps) {
  return (
    <Card style={{ padding: 24 }}>
      <Flex gap="4" align="center" wrap="wrap">
        <InfoItem label="Validation State">
          <ValidationTag nemo={row.nemo} />
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Last Job">
          <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>
            {row.last_job}
          </Text>
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Sinfo">
          <Text kind="body/regular/md">{row.sinfo}</Text>
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Device Status">
          <Text kind="body/regular/md">{row.ct_down_unk}</Text>
        </InfoItem>

        {comment && (
          <>
            <Divider orientation="vertical" />
            <InfoItem label="Comment">
              <Text kind="body/regular/md">{comment}</Text>
            </InfoItem>
          </>
        )}
      </Flex>
    </Card>
  );
}
