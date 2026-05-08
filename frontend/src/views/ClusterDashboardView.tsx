import { useCallback, useState } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import TextField from "@mui/material/TextField";
import { Text } from "@nvidia/foundations-react-core";
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
      <Typography variant="caption" color="text.secondary" sx={{ textTransform: "uppercase", letterSpacing: 0.06 }}>
        {label}
      </Typography>
      <Typography variant="h6" sx={{ mt: 0.5, fontWeight: 600, wordBreak: "break-word", color: "text.primary" }}>
        {value}
      </Typography>
      {hint && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
          {hint}
        </Typography>
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
            <Typography color="error" variant="body2" sx={{ mb: 2 }}>
              {labelsError}
            </Typography>
          )}
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            These values apply only when the corresponding env-derived field is unset or shown as an em dash (—).
            Leave a field empty and save to clear its saved override.
          </Typography>
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
          <Button onClick={() => setLabelsOpen(false)} disabled={labelsSaving}>
            Cancel
          </Button>
          <Button onClick={() => void saveLabels()} variant="contained" disabled={labelsSaving}>
            {labelsSaving ? "Saving…" : "Save"}
          </Button>
        </DialogActions>
      </Dialog>

      {data && (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {s && (
            <Paper variant="outlined" sx={{ p: 2, borderLeft: `4px solid ${NVIDIA_GREEN}` }}>
              <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1 }}>
                Rack validation states (summary)
              </Typography>
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
            <Typography variant="subtitle2" sx={{ color: NVIDIA_GREEN, fontWeight: 700 }}>
              Identity
            </Typography>
            <Typography variant="h5" sx={{ fontWeight: 700, mt: 0.5, color: "text.primary" }}>
              {data.cluster_name}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              Project / customer:{" "}
              <strong style={{ color: NVIDIA_GREEN }}>{data.customer_name}</strong>
            </Typography>
            <Box sx={{ mt: 1.5 }}>
              <Button size="small" variant="outlined" onClick={openLabelsDialog} sx={{ borderColor: NVIDIA_GREEN, color: NVIDIA_GREEN }}>
                Edit cluster labels…
              </Button>
            </Box>
            <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, mt: 2 }}>
              <Paper variant="outlined" sx={{ px: 2, py: 1, borderColor: "rgba(118, 185, 0, 0.5)" }}>
                <Typography variant="caption" color="text.secondary">
                  Dashboard users (approx.)
                </Typography>
                <Typography variant="h4" sx={{ fontWeight: 800, color: NVIDIA_GREEN, lineHeight: 1.1 }}>
                  {connected}
                </Typography>
              </Paper>
              {data.status_updated_at && (
                <Typography variant="caption" color="text.secondary" sx={{ alignSelf: "flex-end" }}>
                  Rack status cache: {data.status_updated_at}
                </Typography>
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
