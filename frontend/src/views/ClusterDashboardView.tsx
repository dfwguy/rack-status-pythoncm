/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import MuiButton from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import TextField from "@mui/material/TextField";
import { RefreshCw } from "lucide-react";
import { Text, Button, Skeleton } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { StatTile, InfoTile } from "../components/dashboard/StatTiles";
import { ErrorBanner } from "../components/ErrorBanner";
import { ClusterSqueueOverview } from "../components/ClusterSqueueOverview";
import { ClusterSnapshotState } from "../components/ClusterSnapshotState";
import { ClusterLiveDetailsState } from "../components/ClusterLiveDetailsState";
import { apiFetch } from "../lib/apiFetch";
import type {
  ClusterDashboardMetadata,
  ClusterOverviewLiveResponse,
  ClusterOverviewResponse,
  ClusterSqueueBlock,
  LiveProbeSection,
} from "../types/api";

const NVFTP_TOKENS_URL =
  "https://nvftp.nvidia.com/workspace/1099ff0b-621c-490f-a662-c69583ccabad/tokens";

const NVIDIA_GREEN = "#76B900";
const NVIDIA_GREEN_DIM = "rgba(118, 185, 0, 0.14)";
const STATUS_RED = "var(--text-color-error, #f44336)";
const STATUS_YELLOW = "#f2c94c";

interface ClusterDashboardViewProps {
  data: ClusterOverviewResponse | null;
  loading: boolean;
  refreshing?: boolean;
  lastUpdated?: number | null;
  error: string | null;
  onRetry?: () => void;
  onClusterMetadataSaved?: () => void;
  liveData?: ClusterOverviewLiveResponse | null;
  liveLoading?: boolean;
  liveError?: string | null;
  onLiveRetry?: () => void;
}

function formatAgo(ts: number): string {
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  return `${hr}h ago`;
}

const emptyMeta = (): ClusterDashboardMetadata => ({
  product_line: "",
  hw_vendor: "",
  customer_name: "",
});

function isConfiguredValue(value: string | undefined): boolean {
  return Boolean(value && value !== "—");
}

function liveStringValue(
  configured: string | undefined,
  section: LiveProbeSection<string> | undefined,
  liveLoading: boolean,
): ReactNode {
  if (section?.state === "fresh" || section?.state === "stale") {
    return section.value ?? (isConfiguredValue(configured) ? configured! : "—");
  }
  if (isConfiguredValue(configured)) return configured!;
  if (section?.state === "initializing" || (liveLoading && !section)) {
    return <Skeleton style={{ width: "10ch", height: "1.2em" }} aria-hidden />;
  }
  return "—";
}

function resolveQueuePresentation(
  base: ClusterSqueueBlock | undefined,
  section: LiveProbeSection<ClusterSqueueBlock> | undefined,
  liveLoading: boolean,
): {
  squeue: ClusterSqueueBlock | null;
  loading: boolean;
  failed: boolean;
} {
  if (section?.state === "fresh" || section?.state === "stale") {
    return { squeue: section.value, loading: false, failed: false };
  }
  if (section?.state === "initializing" || (liveLoading && !section)) {
    return { squeue: null, loading: true, failed: false };
  }
  if (section?.state === "failed") {
    return {
      squeue: section.value ?? { ok: false, stdout: "", error: section.last_error ?? "Queue unavailable" },
      loading: false,
      failed: true,
    };
  }
  if (base?.ok) {
    return { squeue: base, loading: false, failed: false };
  }
  return { squeue: null, loading: liveLoading, failed: !liveLoading };
}

export function ClusterDashboardView({
  data,
  loading,
  refreshing = false,
  lastUpdated = null,
  error,
  onRetry,
  onClusterMetadataSaved,
  liveData = null,
  liveLoading = false,
  liveError = null,
  onLiveRetry,
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
      const res = await apiFetch("/api/cluster-dashboard-metadata", {
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

  const s = data?.validation_summary;
  const connected = data?.connected_users ?? 0;
  const inventoryReady =
    data?.snapshot.state === "fresh" || data?.snapshot.state === "stale";
  const inventoryInitializing = data?.snapshot.state === "initializing";
  const inventoryValue = (n: number | null | undefined) =>
    inventoryReady && n != null ? n : "—";
  const queuePresentation = resolveQueuePresentation(
    data?.squeue,
    liveData?.sections.queue,
    liveLoading,
  );
  const bcmVersion = liveStringValue(data?.bcm_version, liveData?.sections.bcm, liveLoading);
  const slurmVersion = liveStringValue(data?.slurm_version, liveData?.sections.slurm, liveLoading);

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
      <PageHeader contentId="dashboard" title="Overview" />

      {data?.snapshot && (
        <Box sx={{ mb: 2 }}>
          <ClusterSnapshotState snapshot={data.snapshot} onRetry={onRetry} />
        </Box>
      )}

      {(liveData || liveError || liveLoading) && (
        <Box sx={{ mb: 2 }}>
          <ClusterLiveDetailsState
            liveData={liveData}
            liveError={liveError}
            onLiveRetry={onLiveRetry}
          />
        </Box>
      )}

      {data && (refreshing || lastUpdated) && (
        <Box
          sx={{
            display: "flex",
            alignItems: "center",
            gap: 1,
            mb: 1.5,
            color: "var(--text-color-secondary)",
          }}
        >
          {refreshing && (
            <Box
              component="span"
              sx={{
                display: "inline-flex",
                animation: "nvis-overview-spin 0.9s linear infinite",
                "@keyframes nvis-overview-spin": {
                  from: { transform: "rotate(0deg)" },
                  to: { transform: "rotate(360deg)" },
                },
              }}
            >
              <RefreshCw size={14} />
            </Box>
          )}
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {refreshing
              ? "Refreshing… showing last known status"
              : lastUpdated
                ? `Updated ${formatAgo(lastUpdated)}`
                : ""}
          </Text>
        </Box>
      )}

      {error && data && (
        <Box sx={{ mb: 1.5 }}>
          <Text kind="body/regular/sm" style={{ color: STATUS_YELLOW }}>
            Last refresh failed ({error}). Showing last known status.
          </Text>
        </Box>
      )}

      {error && !data && <ErrorBanner message={error} />}

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
                gridTemplateColumns: {
                  xs: "1fr",
                  sm: "repeat(2, minmax(0, 1fr))",
                  md: "repeat(3, minmax(0, 1fr))",
                  lg: "repeat(5, minmax(0, 1fr))",
                },
                gap: 2,
              }}
            >
              <StatTile label="Racks" value={inventoryValue(data.rack_count)} unit="total" />
              <StatTile label="Nodes" value={inventoryValue(data.tray_hosts)} unit="trays" />
              {inventoryReady && s ? (
                <>
                  <StatTile label="Done" value={s.done} accentColor={NVIDIA_GREEN} unit="racks" />
                  <StatTile label="Failed" value={s.fail} accentColor={s.fail > 0 ? STATUS_RED : undefined} unit="racks" />
                  <StatTile label="Ongoing" value={s.ongoing} accentColor={s.ongoing > 0 ? STATUS_YELLOW : undefined} unit="racks" />
                  <StatTile label="Pending" value={s.pending} unit="racks" />
                  {s.stage1 && (
                    <StatTile
                      label="Stage 1 · NCCL"
                      value={s.stage1.done}
                      unit={`/ ${data.rack_count} done`}
                      accentColor={s.stage1.done > 0 ? NVIDIA_GREEN : undefined}
                      caption={`${s.stage1.ongoing} running · ${s.stage1.fail} fail`}
                    />
                  )}
                  {s.stage2 && (
                    <StatTile
                      label="Stage 2 · NeMo"
                      value={s.stage2.done}
                      unit={`/ ${data.rack_count} done`}
                      accentColor={s.stage2.done > 0 ? NVIDIA_GREEN : undefined}
                      caption={`${s.stage2.ongoing} running · ${s.stage2.fail} fail`}
                    />
                  )}
                  <StatTile label="Dashboard users" value={connected} unit="online" />
                </>
              ) : (
                <>
                  {(["Done", "Failed", "Ongoing"] as const).map((label) => (
                    <StatTile
                      key={label}
                      label={label}
                      value={
                        inventoryInitializing ? (
                          <Skeleton style={{ width: "3ch", height: "1.2em" }} aria-hidden />
                        ) : (
                          "—"
                        )
                      }
                      unit="racks"
                    />
                  ))}
                </>
              )}
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
              <InfoTile label="BCM version" value={bcmVersion} mono hint='RACK_CLUSTER_BCM_VERSION, else cmsh versioninfo' />
              <InfoTile label="Slurm version" value={slurmVersion} mono hint="RACK_CLUSTER_SLURM_VERSION, else Slurm on head" />
              <InfoTile label="NEMO container" value={data.nemo_container} mono hint="RACK_CLUSTER_NEMO_* or NEMO_SQSH_SRC" />
              <InfoTile label="Dashboard build" value={data.app_version} mono hint="var/build-info.json from the image build, else version.py" />
            </Box>
          </Box>

          <ClusterSqueueOverview
            squeue={queuePresentation.squeue}
            loading={queuePresentation.loading}
            failed={queuePresentation.failed}
          />
        </Box>
      )}

      {loading && !data && (
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
                gridTemplateColumns: {
                  xs: "1fr",
                  sm: "repeat(2, minmax(0, 1fr))",
                  md: "repeat(3, minmax(0, 1fr))",
                  lg: "repeat(5, minmax(0, 1fr))",
                },
                gap: 2,
              }}
            >
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} style={{ width: "100%", height: 88 }} aria-hidden />
              ))}
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
            <Skeleton style={{ width: "40%", height: 24, marginBottom: 12 }} aria-hidden />
            <Skeleton style={{ width: "60%", height: 32, marginBottom: 8 }} aria-hidden />
            <Skeleton style={{ width: "50%", height: 20 }} aria-hidden />
          </Paper>
          <ClusterSqueueOverview squeue={null} loading={liveLoading || !liveData} />
        </Box>
      )}
    </Box>
  );
}
