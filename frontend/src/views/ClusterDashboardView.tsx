import { useCallback, useEffect, useState } from "react";
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
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { StatTile, InfoTile } from "../components/dashboard/StatTiles";
import { LoadingSkeleton } from "../components/LoadingSkeleton";
import { ErrorBanner } from "../components/ErrorBanner";
import { ClusterSqueueOverview } from "../components/ClusterSqueueOverview";
import { apiFetch } from "../lib/apiFetch";
import type { ClusterDashboardMetadata, ClusterOverviewResponse } from "../types/api";

const NVFTP_TOKENS_URL =
  "https://nvftp.nvidia.com/workspace/1099ff0b-621c-490f-a662-c69583ccabad/tokens";

const NVIDIA_GREEN = "#76B900";
const NVIDIA_GREEN_DIM = "rgba(118, 185, 0, 0.14)";
const STATUS_RED = "var(--text-color-error, #f44336)";
const STATUS_YELLOW = "#f2c94c";

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

  const [nvftpDialogOpen, setNvftpDialogOpen] = useState(false);
  const [nvftpSftpUser, setNvftpSftpUser] = useState("");
  const [nvftpProjectToken, setNvftpProjectToken] = useState("");
  const [nvftpTokenInput, setNvftpTokenInput] = useState("");
  const [nvftpSaving, setNvftpSaving] = useState(false);
  const [nvftpTesting, setNvftpTesting] = useState(false);
  const [nvftpTestMessage, setNvftpTestMessage] = useState<string | null>(null);
  const [nvftpError, setNvftpError] = useState<string | null>(null);
  const [nvftpConfigured, setNvftpConfigured] = useState(false);
  const [nvftpMasked, setNvftpMasked] = useState<string | null>(null);
  const [nvftpTokensUrl, setNvftpTokensUrl] = useState(NVFTP_TOKENS_URL);
  const [nvftpEnvSaving, setNvftpEnvSaving] = useState(false);
  const [nvftpEnvMessage, setNvftpEnvMessage] = useState<string | null>(null);
  const [nvftpSaveMessage, setNvftpSaveMessage] = useState<string | null>(null);
  const [nvftpSyncStatus, setNvftpSyncStatus] = useState<{
    enabled?: boolean;
    skip_reason?: string | null;
    last_run_at?: string;
    interval_sec?: number;
  } | null>(null);

  const monoField = {
    input: { sx: { fontFamily: "ui-monospace, monospace", fontSize: "0.8125rem" } },
  } as const;

  const refreshNvftpStatus = useCallback(async () => {
    try {
      const res = await apiFetch("/api/vault/nvftp-token", { cache: "no-store" });
      if (!res.ok) return;
      const json = (await res.json()) as {
        ok?: boolean;
        configured?: boolean;
        masked?: string;
        tokens_url?: string;
      };
      if (json.ok) {
        setNvftpConfigured(Boolean(json.configured));
        setNvftpMasked(json.masked ?? null);
        if (json.tokens_url) setNvftpTokensUrl(json.tokens_url);
      }
    } catch {
      /* ignore */
    }
  }, []);

  const refreshNvftpSyncStatus = useCallback(async () => {
    try {
      const res = await apiFetch("/api/nvftp-sync/status", { cache: "no-store" });
      if (!res.ok) return;
      const json = (await res.json()) as {
        ok?: boolean;
        enabled?: boolean;
        skip_reason?: string | null;
        last_run_at?: string;
        interval_sec?: number;
      };
      if (json.ok) setNvftpSyncStatus(json);
    } catch {
      /* ignore */
    }
  }, []);

  const loadNvftpJobEnvFields = useCallback(async () => {
    try {
      const res = await apiFetch("/api/settings/job-environment", { cache: "no-store" });
      if (!res.ok) return;
      const json = (await res.json()) as {
        ok?: boolean;
        sections?: { variables: { key: string; value?: string }[] }[];
        variables?: { key: string; value?: string }[];
      };
      if (!json.ok) return;
      const rows =
        json.sections?.flatMap((s) => s.variables) ?? json.variables ?? [];
      const find = (key: string) => rows.find((v) => v.key === key)?.value ?? "";
      setNvftpSftpUser(find("RACK_NVFTP_SFTP_USER"));
      setNvftpProjectToken(find("RACK_PROJECT_TOKEN"));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void refreshNvftpStatus();
    void loadNvftpJobEnvFields();
    void refreshNvftpSyncStatus();
  }, [refreshNvftpStatus, refreshNvftpSyncStatus, loadNvftpJobEnvFields]);

  const saveNvftpEnvSettings = useCallback(async (user: string, project: string) => {
    const envRes = await apiFetch("/api/settings/job-environment", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overrides: {
          RACK_NVFTP_SFTP_USER: user,
          RACK_PROJECT_TOKEN: project,
        },
      }),
    });
    const envJson = (await envRes.json()) as { ok?: boolean; error?: string };
    if (!envRes.ok || !envJson.ok) {
      throw new Error(envJson.error || `Settings save failed (HTTP ${envRes.status})`);
    }
    void refreshNvftpSyncStatus();
  }, [refreshNvftpSyncStatus]);

  const saveNvftpProjectFromOverview = useCallback(async () => {
    setNvftpEnvSaving(true);
    setNvftpEnvMessage(null);
    setNvftpError(null);
    try {
      const project = nvftpProjectToken.trim();
      if (!project) throw new Error("RACK_PROJECT_TOKEN is required");
      await saveNvftpEnvSettings(nvftpSftpUser.trim(), project);
      setNvftpEnvMessage("Project folder and SFTP user saved.");
    } catch (e) {
      setNvftpEnvMessage(null);
      setNvftpError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setNvftpEnvSaving(false);
    }
  }, [nvftpProjectToken, nvftpSftpUser, saveNvftpEnvSettings]);

  const openProjectViewDialog = useCallback(() => {
    setNvftpError(null);
    setNvftpTestMessage(null);
    setNvftpSaveMessage(null);
    setNvftpDialogOpen(true);
  }, []);

  const openNvftpTokensPage = useCallback(() => {
    window.open(nvftpTokensUrl, "_blank", "noopener,noreferrer");
  }, [nvftpTokensUrl]);

  const testNvftpConnection = useCallback(async () => {
    setNvftpTesting(true);
    setNvftpError(null);
    setNvftpTestMessage(null);
    try {
      const user = nvftpSftpUser.trim();
      const project = nvftpProjectToken.trim();
      const token = nvftpTokenInput.trim() || undefined;
      if (!user) throw new Error("RACK_NVFTP_SFTP_USER is required");
      if (!project) throw new Error("RACK_PROJECT_TOKEN is required");
      if (!token && !nvftpConfigured) {
        throw new Error("Paste the NVFTP token or save one first");
      }
      const res = await apiFetch("/api/nvftp-sync/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_token: project,
          sftp_user: user,
          token,
        }),
      });
      const json = (await res.json()) as {
        ok?: boolean;
        error?: string;
        message?: string;
        remote_dir?: string;
        remote_file?: string;
      };
      if (!res.ok || !json.ok) {
        throw new Error(json.error || json.message || `HTTP ${res.status}`);
      }
      setNvftpTestMessage(
        json.message
          ? `${json.message}${json.remote_file ? ` (${json.remote_file})` : ""}`
          : "Test file uploaded successfully."
      );
    } catch (e) {
      setNvftpError(e instanceof Error ? e.message : "Test failed");
    } finally {
      setNvftpTesting(false);
    }
  }, [nvftpSftpUser, nvftpProjectToken, nvftpTokenInput, nvftpConfigured]);

  const saveNvftpToken = useCallback(async () => {
    setNvftpSaving(true);
    setNvftpError(null);
    setNvftpSaveMessage(null);
    try {
      const user = nvftpSftpUser.trim();
      const project = nvftpProjectToken.trim();
      const token = nvftpTokenInput.trim();
      if (!user) {
        throw new Error("RACK_NVFTP_SFTP_USER is required");
      }
      if (!project) {
        throw new Error("RACK_PROJECT_TOKEN is required");
      }
      await saveNvftpEnvSettings(user, project);
      if (token) {
        const res = await apiFetch("/api/vault/nvftp-token", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        const json = (await res.json()) as { ok?: boolean; error?: string; configured?: boolean; masked?: string };
        if (!res.ok || !json.ok) {
          throw new Error(json.error || `HTTP ${res.status}`);
        }
        setNvftpConfigured(Boolean(json.configured));
        setNvftpMasked(json.masked ?? null);
      }
      setNvftpSaveMessage("Saved. You can run Test without closing this dialog.");
      void refreshNvftpSyncStatus();
    } catch (e) {
      setNvftpError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setNvftpSaving(false);
    }
  }, [nvftpTokenInput, nvftpSftpUser, nvftpProjectToken, saveNvftpEnvSettings, refreshNvftpSyncStatus]);

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
        maxWidth: CONTENT_MAX_WIDTH.comfortable,
        mx: "auto",
        p: 4,
        borderTop: `4px solid ${NVIDIA_GREEN}`,
        minHeight: "60vh",
      }}
    >
      <PageHeader title="Overview" />

      {error && <ErrorBanner message={error} />}

      <Dialog
        open={nvftpDialogOpen}
        onClose={() => !nvftpSaving && !nvftpTesting && setNvftpDialogOpen(false)}
        fullWidth
        maxWidth="sm"
      >
        <DialogTitle>Set Project View</DialogTitle>
        <DialogContent>
          <Text kind="body/regular/md" style={{ display: "block", marginBottom: 16, color: "var(--text-color-secondary)" }}>
            Configure the NVFTP project folder and SFTP username for cache sync to transfer.nvftp.nvidia.com.
            The token from Generate NVFTP token is stored encrypted and used as the SFTP password with your username.
          </Text>
          {nvftpSaveMessage && (
            <Text kind="body/regular/md" style={{ display: "block", marginBottom: 16, color: NVIDIA_GREEN }}>
              {nvftpSaveMessage}
            </Text>
          )}
          {nvftpConfigured && nvftpMasked && (
            <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)" }}>
              Current token: {nvftpMasked} (save again to replace)
            </Text>
          )}
          {nvftpTestMessage && (
            <Text kind="body/regular/md" style={{ display: "block", marginBottom: 16, color: NVIDIA_GREEN }}>
              {nvftpTestMessage}
            </Text>
          )}
          {nvftpError && (
            <Text kind="body/regular/md" style={{ display: "block", marginBottom: 16, color: "var(--text-color-error, #f44336)" }}>
              {nvftpError}
            </Text>
          )}
          <TextField
            label="Project folder (RACK_PROJECT_TOKEN)"
            value={nvftpProjectToken}
            onChange={(e) => setNvftpProjectToken(e.target.value)}
            fullWidth
            margin="normal"
            autoComplete="off"
            placeholder="Remote directory name for this site"
            helperText="Mirrors var/cache/ and rack_comments.json to /nvis-project-view/{this value}/cache/ on NVFTP."
            slotProps={monoField}
          />
          <TextField
            label="NVFTP SFTP username (RACK_NVFTP_SFTP_USER)"
            value={nvftpSftpUser}
            onChange={(e) => setNvftpSftpUser(e.target.value)}
            fullWidth
            margin="normal"
            autoComplete="off"
            placeholder="e.g. nv-ssh-… from your NVFTP workspace"
            slotProps={monoField}
          />
          <TextField
            label="Paste the token here"
            type="password"
            value={nvftpTokenInput}
            onChange={(e) => setNvftpTokenInput(e.target.value)}
            fullWidth
            margin="normal"
            autoComplete="new-password"
            placeholder="Paste NVFTP token"
            slotProps={monoField}
          />
        </DialogContent>
        <DialogActions sx={{ flexWrap: "wrap", gap: 1, px: 2, pb: 2 }}>
          <MuiButton onClick={openNvftpTokensPage} disabled={nvftpSaving || nvftpTesting}>
            Generate NVFTP token
          </MuiButton>
          <Box sx={{ flex: 1 }} />
          <MuiButton onClick={() => setNvftpDialogOpen(false)} disabled={nvftpSaving || nvftpTesting}>
            Close
          </MuiButton>
          <MuiButton
            onClick={() => void testNvftpConnection()}
            variant="outlined"
            disabled={
              nvftpSaving ||
              nvftpTesting ||
              !nvftpSftpUser.trim() ||
              !nvftpProjectToken.trim() ||
              (!nvftpTokenInput.trim() && !nvftpConfigured)
            }
          >
            {nvftpTesting ? "Testing…" : "Test"}
          </MuiButton>
          <MuiButton
            onClick={() => void saveNvftpToken()}
            variant="contained"
            disabled={nvftpSaving || nvftpTesting || !nvftpSftpUser.trim() || !nvftpProjectToken.trim()}
          >
            {nvftpSaving ? "Saving…" : "Save"}
          </MuiButton>
        </DialogActions>
      </Dialog>

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
          <Box>
            <Text
              kind="body/bold/sm"
              style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)", textTransform: "uppercase", letterSpacing: 0.06 }}
            >
              Cluster statistics
            </Text>
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                gap: 2,
              }}
            >
              <StatTile label="Racks" value={data.rack_count} unit="total" />
              <StatTile label="Nodes" value={data.tray_hosts} unit="trays" />
              {s && <StatTile label="Done" value={s.done} accentColor={NVIDIA_GREEN} unit="racks" />}
              {s && <StatTile label="Failed" value={s.fail} accentColor={s.fail > 0 ? STATUS_RED : undefined} unit="racks" />}
              {s && <StatTile label="Ongoing" value={s.ongoing} accentColor={s.ongoing > 0 ? STATUS_YELLOW : undefined} unit="racks" />}
              {s && <StatTile label="Pending" value={s.pending} unit="racks" />}
              <StatTile label="Dashboard users" value={connected} unit="online" />
            </Box>
          </Box>

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

            <Box sx={{ mt: 2.5, pt: 2, borderTop: "1px solid rgba(118, 185, 0, 0.35)" }}>
              <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8, color: NVIDIA_GREEN }}>
                NVFTP cache sync
              </Text>
              <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)" }}>
                Remote folder for <Box component="span" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>var/cache</Box> uploads.
                {nvftpConfigured && nvftpMasked ? ` Token: ${nvftpMasked}.` : " No NVFTP token saved yet."}
                {nvftpSyncStatus?.last_run_at
                  ? ` Last sync: ${new Date(nvftpSyncStatus.last_run_at).toLocaleString()}.`
                  : ""}
                {nvftpSyncStatus?.enabled
                  ? ` Interval: ${Math.round((nvftpSyncStatus.interval_sec ?? 7200) / 3600)}h.`
                  : nvftpSyncStatus?.skip_reason
                    ? ` (${nvftpSyncStatus.skip_reason})`
                    : ""}
              </Text>
              {nvftpEnvMessage && (
                <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 8, color: NVIDIA_GREEN }}>
                  {nvftpEnvMessage}
                </Text>
              )}
              <Box
                sx={{
                  display: "grid",
                  gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
                  gap: 2,
                  alignItems: "start",
                }}
              >
                <TextField
                  label="Project folder (RACK_PROJECT_TOKEN)"
                  value={nvftpProjectToken}
                  onChange={(e) => setNvftpProjectToken(e.target.value)}
                  fullWidth
                  size="small"
                  autoComplete="off"
                  placeholder="e.g. mistral-p2"
                  slotProps={monoField}
                />
                <TextField
                  label="NVFTP SFTP username"
                  value={nvftpSftpUser}
                  onChange={(e) => setNvftpSftpUser(e.target.value)}
                  fullWidth
                  size="small"
                  autoComplete="off"
                  placeholder="nv-ssh-…"
                  slotProps={monoField}
                />
              </Box>
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1.5, mt: 1.5 }}>
                <MuiButton
                  size="small"
                  variant="outlined"
                  disabled={nvftpEnvSaving || !nvftpProjectToken.trim()}
                  onClick={() => void saveNvftpProjectFromOverview()}
                >
                  {nvftpEnvSaving ? "Saving…" : "Save folder settings"}
                </MuiButton>
                <Button size="small" kind="secondary" onClick={openProjectViewDialog}>
                  Set Project View
                </Button>
              </Box>
            </Box>

            {data.status_updated_at && (
              <Box sx={{ mt: 2 }}>
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  Rack status cache: {data.status_updated_at}
                </Text>
              </Box>
            )}
          </Paper>

          <Box>
            <Text
              kind="body/bold/sm"
              style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)", textTransform: "uppercase", letterSpacing: 0.06 }}
            >
              Environment & versions
            </Text>
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", md: "repeat(3, 1fr)" },
                gap: 2,
              }}
            >
              <InfoTile label="Product line" value={data.product_line} hint="RACK_CLUSTER_PRODUCT_LINE" />
              <InfoTile label="HW vendor" value={data.hw_vendor} hint="RACK_CLUSTER_HW_VENDOR" />
              <InfoTile label="BCM version" value={data.bcm_version} mono hint='RACK_CLUSTER_BCM_VERSION, else cmsh versioninfo' />
              <InfoTile label="Slurm version" value={data.slurm_version} mono hint="RACK_CLUSTER_SLURM_VERSION, else Slurm on head" />
              <InfoTile label="NEMO container" value={data.nemo_container} mono hint="RACK_CLUSTER_NEMO_* or NEMO_SQSH_SRC" />
              <InfoTile label="Dashboard build" value={data.app_version} mono hint="RACK_WEB_VERSION" />
            </Box>
          </Box>

          <ClusterSqueueOverview squeue={data.squeue} />
        </Box>
      )}
    </Box>
  );
}
