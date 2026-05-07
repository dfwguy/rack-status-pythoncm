import Box from "@mui/material/Box";
import { Card, Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";

export function ReportsView() {
  return (
    <Box sx={{ maxWidth: 1400, mx: "auto", p: 4 }}>
      <PageHeader
        title="Reports"
        description="Download bug reports and log archives from cluster nodes."
      />

      <Card style={{ padding: 48, textAlign: "center" }}>
        <Text kind="body/bold/md" style={{ color: "var(--text-color-secondary)", marginBottom: 8, display: "block" }}>
          Bug Report Downloads
        </Text>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          Connect to /api/bug-report-inventory to list available report archives.
        </Text>
      </Card>
    </Box>
  );
}
