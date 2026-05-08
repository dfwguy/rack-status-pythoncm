import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import { Card, Text, Button } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";

interface SettingsViewProps {
  logDir: string;
  onLogDirChange: (val: string) => void;
}

export function SettingsView({ logDir, onLogDirChange }: SettingsViewProps) {
  return (
    <Box sx={{ maxWidth: 800, mx: "auto", p: 4 }}>
      <PageHeader
        title="Settings"
        description="Configure dashboard behavior and connection parameters."
      />

      <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
        <Card style={{ padding: 24 }}>
          <Text kind="body/bold/md" style={{ marginBottom: 8, display: "block" }}>
            Log Directory
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 16, display: "block" }}>
            Root directory for NEMO validation logs. This is used as the working directory
            for rack_status.sh and log analysis.
          </Text>
          <Box sx={{ display: "flex", gap: 2, alignItems: "center" }}>
            <TextField
              size="small"
              fullWidth
              value={logDir}
              onChange={(e) => onLogDirChange(e.target.value)}
              placeholder="leave empty for server default (RACK_STATUS_LOG_DIR)"
            />
            <Button kind="secondary" size="small">
              Apply
            </Button>
          </Box>
        </Card>

        <Card style={{ padding: 24 }}>
          <Text kind="body/bold/md" style={{ marginBottom: 8, display: "block" }}>
            Refresh Interval
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 16, display: "block" }}>
            How often the dashboard polls /api/status for updated rack data.
            Default: 15 seconds.
          </Text>
          <Box sx={{ display: "flex", gap: 2, alignItems: "center" }}>
            <TextField
              size="small"
              defaultValue="15"
              sx={{ width: 100 }}
            />
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              seconds
            </Text>
          </Box>
        </Card>
      </Box>
    </Box>
  );
}
