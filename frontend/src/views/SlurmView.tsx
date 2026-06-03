import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { SqueuePanel } from "../components/SqueuePanel";
import { SinfoPanel } from "../components/SinfoPanel";

interface SlurmViewProps {
  logDir?: string;
}

export function SlurmView({ logDir }: SlurmViewProps) {
  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader title="Slurm" description="Live squeue and sinfo -R --long from the dashboard host (same APIs as the legacy toolbar)." />

      <Text kind="body/regular/sm" style={{ marginBottom: 16, display: "block", color: "var(--text-color-secondary)" }}>
        Job log tail uses the log directory from settings (or server default) for path validation, same as the legacy squeue dialog.
      </Text>

      <Box sx={{ display: "flex", flexDirection: "column", gap: 2, alignItems: "stretch" }}>
        <SqueuePanel logDir={logDir} active tableMaxHeight={560} />
        <SinfoPanel active tableMaxHeight={560} />
      </Box>
    </Box>
  );
}
