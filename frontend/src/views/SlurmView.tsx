import Box from "@mui/material/Box";
import { Card, Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";

export function SlurmView() {
  return (
    <Box sx={{ maxWidth: 1400, mx: "auto", p: 4 }}>
      <PageHeader
        title="Slurm"
        description="View squeue and sinfo outputs from the cluster."
      />

      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2 }}>
        <Card style={{ padding: 24 }}>
          <Text kind="body/bold/md" style={{ marginBottom: 8, display: "block" }}>
            squeue
          </Text>
          <div style={{ backgroundColor: "var(--background-color-surface-base)", borderRadius: 4, padding: 16, fontFamily: "monospace" }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Connect to /api/slurm-squeue
            </Text>
          </div>
        </Card>

        <Card style={{ padding: 24 }}>
          <Text kind="body/bold/md" style={{ marginBottom: 8, display: "block" }}>
            sinfo -R
          </Text>
          <div style={{ backgroundColor: "var(--background-color-surface-base)", borderRadius: 4, padding: 16, fontFamily: "monospace" }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Connect to /api/slurm-sinfo-r
            </Text>
          </div>
        </Card>
      </Box>
    </Box>
  );
}
