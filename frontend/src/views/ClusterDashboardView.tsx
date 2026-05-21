import { useCallback, useState } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import MuiButton from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import TextField from "@mui/material/TextField";
import { Text, Button } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { LoadingSkeleton } from "../components/LoadingSkeleton";
import { ErrorBanner } from "../components/ErrorBanner";
import { ClusterSqueueOverview } from "../components/ClusterSqueueOverview";
import type { ClusterDashboardMetadata, ClusterOverviewResponse } from "../types/api";

const NVIDIA_GREEN = "#76B900";
const NVIDIA_GREEN_DIM = "rgba(118, 185, 0, 0.14)";

interface ClusterDashboardViewProps {
  data: ClusterOverviewResponse | null;
  loading: boolean;
  error: string | null;
  onClusterMetadataSaved?: () => void;
}

const emptyMeta = (): ClusterDashboardMetadata => ({
  product_line: "",
  hw_vendor: "",
  customer_name: "",
});

function MetricCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <Paper
      variant="outlined"
      sx={{
        p: 2,
        height: "100%",
        borderTop: `3px solid ${NVIDIA_GREEN}`,
        bgcolor: NVIDIA_GREEN_DIM,
      }}
    >
      <Text kind="body/regular/sm" style={{ display: "block", textTransform: "uppercase", letterSpacing: 0.06, color: "var(--text-color-secondary)" }}>
        {label}
      </Text>
      <Text kind="title/sm" style={{ display: "block", marginTop: 4, fontWeight: 600, wordBreak: "break-word" }}>
        {value}
      </Text>
      {hint && (
        <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4, color: "var(--text-color-secondary)" }}>
          {hint}
        </Text>
      )}
    </Paper>
  );
}

export function ClusterDashboardView({
  data,
  loading,
  error,
  onClusterMetadataSaved,
}: ClusterDashboardViewProps) {
  const [labelsOpen, setLabelsOpen] = useState(false);
  const [productLine, setProductLine] = useState("");
  const [hwVendor, setHwVendor] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [labelsSaving, setLabelsSaving] = useState(false);
  const [labelsError, setLabelsError] = useState<string | null>(null);

  const openLabelsDialog = useCallback(() => {
    const m = data?.cluster_dashboard_metadata ?? emptyMeta();
    setProductLine(m.product_line);
    setHwVendor(m.hw_vendor);
    setCustomerName(m.customer_name);
    setLabelsError(null);
    setLabelsOpen(true);
  }, [data]);

  const saveLabels = useCallback(async () => {
    setLabelsSaving(true);
    setLabelsError(null);
    try {
      const res = await fetch("/api/cluster-dashboard-metadata", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          product_line: productLine,
          hw_vendor: hwVendor,
          customer_name: customerName,
        }),
      });
      const json = (await res.json()) as { ok: boolean; error?: string };
      if (!res.ok || !json.ok) {
        throw new Error(json.error || `HTTP ${res.status}`);
      }
      setLabelsOpen(false);
      onClusterMetadataSaved?.();
    } catch (e) {
      setLabelsError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setLabelsSaving(false);
    }
  }, [productLine, hwVendor, customerName, onClusterMetadataSaved]);

  if (loading && !data) return <LoadingSkeleton />;

  const s = data?.validation_summary;
  const connected = data?.connected_users ?? 0;

  return (
    <Box
      sx={{
        maxWidth: 1200,
        mx: "auto",
        p: 4,
        borderTop: `4px solid ${NVIDIA_GREEN}`,
        minHeight: "60vh",
      }}
    >
      <PageHeader title="Cluster overview" />

      {error && <ErrorBanner message={error} />}

      <Dialog open={labelsOpen} onClose={() => !labelsSaving && setLabelsOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>Cluster labels (dashboard)</DialogTitle>
        <DialogContent>
          {labelsError && (
            <Text kind="body/regular/md" style={{ display: "block", marginBottom: 16, color: "var(--text-color-error, #f44336)" }}>
              {labelsError}
            </Text>
          )}
          <Text kind="body/regular/md" style={{ display: "block", marginBottom: 16, color: "var(--text-color-secondary)" }}>
            These values apply only when the corresponding env-derived field is unset or shown as an em dash (—).
            Leave a field empty and save to clear its saved override.
          </Text>
          <TextField
            label="Product line"
            value={productLine}
            onChange={(e) => setProductLine(e.target.value)}
            fullWidth
            margin="normal"
            autoComplete="off"
          />
          <TextField
            label="HW vendor"
            value={hwVendor}
            onChange={(e) => setHwVendor(e.target.value)}
            fullWidth
            margin="normal"
            autoComplete="off"
          />
          <TextField
            label="Project / customer"
            value={customerName}
            onChange={(e) => setCustomerName(e.target.value)}
            fullWidth
            margin="normal"
            autoComplete="off"
          />
        </DialogContent>
        <DialogActions>
          <MuiButton variant="outlined" onClick={() => setLabelsOpen(false)} disabled={labelsSaving}>
            Cancel
          </MuiButton>
          <MuiButton onClick={() => void saveLabels()} variant="contained" disabled={labelsSaving}>
            {labelsSaving ? "Saving…" : "Save"}
          </MuiButton>
        </DialogActions>
      </Dialog>

      {data && (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {s && (
            <Paper variant="outlined" sx={{ p: 2, borderLeft: `4px solid ${NVIDIA_GREEN}` }}>
              <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8, color: "var(--text-color-secondary)" }}>
                Rack validation states (summary)
              </Text>
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2 }}>
                <Text kind="body/regular/sm">Done: <strong style={{ color: NVIDIA_GREEN }}>{s.done}</strong></Text>
                <Text kind="body/regular/sm">Fail: <strong>{s.fail}</strong></Text>
                <Text kind="body/regular/sm">Ongoing: <strong>{s.ongoing}</strong></Text>
                <Text kind="body/regular/sm">Pending: <strong>{s.pending}</strong></Text>
              </Box>
            </Paper>
          )}

          <Paper
            elevation={0}
            sx={{
              p: 2.5,
              border: `1px solid rgba(118, 185, 0, 0.45)`,
              background: `linear-gradient(135deg, ${NVIDIA_GREEN_DIM} 0%, transparent 55%)`,
            }}
          >
            <Text kind="body/bold/sm" style={{ display: "block", color: NVIDIA_GREEN, fontWeight: 700 }}>
              Identity
            </Text>
            <Text kind="title/lg" style={{ display: "block", fontWeight: 700, marginTop: 4 }}>
              {data.cluster_name}
            </Text>
            <Text kind="body/regular/md" style={{ display: "block", marginTop: 4, color: "var(--text-color-secondary)" }}>
              Project / customer:{" "}
              <strong style={{ color: NVIDIA_GREEN }}>{data.customer_name}</strong>
            </Text>
            <Box sx={{ mt: 1.5 }}>
              <Button size="small" kind="secondary" onClick={openLabelsDialog} style={{ borderColor: NVIDIA_GREEN, color: NVIDIA_GREEN }}>
                Edit cluster labels…
              </Button>
            </Box>
            <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, mt: 2 }}>
              <Paper variant="outlined" sx={{ px: 2, py: 1, borderColor: "rgba(118, 185, 0, 0.5)" }}>
                <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
                  Dashboard users (approx.)
                </Text>
                <Text kind="display/sm" style={{ display: "block", fontWeight: 800, color: NVIDIA_GREEN, lineHeight: 1.1 }}>
                  {connected}
                </Text>
              </Paper>
              {data.status_updated_at && (
                <Text kind="body/regular/sm" style={{ alignSelf: "flex-end", color: "var(--text-color-secondary)" }}>
                  Rack status cache: {data.status_updated_at}
                </Text>
              )}
            </Box>
          </Paper>

          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", md: "repeat(3, 1fr)" },
              gap: 2,
            }}
          >
            <MetricCard label="Product line" value={data.product_line} hint="RACK_CLUSTER_PRODUCT_LINE" />
            <MetricCard label="HW vendor" value={data.hw_vendor} hint="RACK_CLUSTER_HW_VENDOR" />
            <MetricCard
              label="BCM version"
              value={data.bcm_version}
              hint='RACK_CLUSTER_BCM_VERSION, else cmsh -c "main; versioninfo"'
            />
            <MetricCard label="Slurm version" value={data.slurm_version} hint="RACK_CLUSTER_SLURM_VERSION, else Slurm on head" />
            <MetricCard label="NEMO container" value={data.nemo_container} hint="RACK_CLUSTER_NEMO_* or NEMO_SQSH_SRC" />
            <MetricCard label="Dashboard build" value={data.app_version} hint="RACK_WEB_VERSION" />
          </Box>

          <ClusterSqueueOverview squeue={data.squeue} />
        </Box>
      )}
    </Box>
  );
}
