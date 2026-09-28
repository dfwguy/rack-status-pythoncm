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
import { useState, useMemo, lazy, Suspense } from "react";
import AppBar from "@mui/material/AppBar";
import MuiToolbar from "@mui/material/Toolbar";
import IconButton from "@mui/material/IconButton";
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import CircularProgress from "@mui/material/CircularProgress";
import LinearProgress from "@mui/material/LinearProgress";
import Divider from "@mui/material/Divider";
import Popover from "@mui/material/Popover";
import { Text, Tag, Flex } from "@nvidia/foundations-react-core";
import {
  Sun,
  Moon,
  RefreshCw,
  Menu,
  Download,
  FileSpreadsheet,
  ListOrdered,
  Server,
  LogOut,
  Users,
  Terminal,
} from "lucide-react";
import { ConnectionStatusIndicator } from "./ConnectionStatusIndicator";
import { SqueueDialog } from "./SqueueDialog";
import { SinfoDialog } from "./SinfoDialog";
// Heavy dialogs are loaded on demand, and the *gating* below is as load-bearing as the
// `lazy()`. A conditionally rendered component is still statically imported unless it is
// lazy, and a lazy component fetches its chunk the moment it is first rendered -- so
// `<Lazy open={false} />` would download everything at first paint and gain nothing. Both
// halves are required: `lazy()` to split the chunk, and `flag && ...` so it stays unfetched
// until the operator asks. See the note in App.tsx.
// xterm + its WebGL addon (~461 kB), for the head-node console.
const WebTerminalDialog = lazy(() => import("./rack/WebTerminalDialog"));
import { useAuth } from "../context/AuthContext";
import { apiDownload } from "../lib/apiFetch";
import { useRackLocksContext } from "../context/RackLocksContext";
import { ProjectSwitcher } from "./ProjectSwitcher";

interface ToolbarProps {
  title: string;
  theme: "dark" | "light";
  onToggleTheme: () => void;
  onRefresh: () => void;
  loading: boolean;
  sidebarOpen: boolean;
  onMenuToggle: () => void;
  activeUsers?: number;
  logDir?: string;
  /** Called after the active project/cluster is switched (refresh dashboards). */
  onClusterSwitched?: () => void;
  /** UI ↔ backend link state for the connection indicator. */
  backendConnected?: boolean;
  /** Backend ↔ cluster (SSH) link state for the connection indicator. */
  clusterReachable?: boolean;
  execMode?: "ssh" | "local";
  lastUpdated?: number | null;
  onReconnect?: () => void;
}

export function Toolbar({
  title,
  theme,
  onToggleTheme,
  onRefresh,
  loading,
  sidebarOpen,
  onMenuToggle,
  activeUsers,
  logDir,
  onClusterSwitched,
  backendConnected = true,
  clusterReachable = true,
  execMode = "ssh",
  lastUpdated = null,
  onReconnect,
}: ToolbarProps) {
  const { user, logout } = useAuth();
  const [squeueOpen, setSqueueOpen] = useState(false);
  const [sinfoOpen, setSinfoOpen] = useState(false);
  const [headTerminalOpen, setHeadTerminalOpen] = useState(false);
  const [excelExporting, setExcelExporting] = useState(false);
  const [usersAnchor, setUsersAnchor] = useState<HTMLElement | null>(null);

  const { locks, me } = useRackLocksContext();
  /** Group lock entries by operator so we can show "alice — 2 racks (1 active, 1 claim)". */
  const operatorActivity = useMemo(() => {
    const byOp = new Map<
      string,
      { operator: string; racks: string[]; activeCount: number; claimCount: number }
    >();
    for (const [rack, lock] of Object.entries(locks)) {
      const op = (lock.operator || "").trim() || "(unknown)";
      const cur = byOp.get(op) || { operator: op, racks: [], activeCount: 0, claimCount: 0 };
      cur.racks.push(rack);
      if (lock.kind === "claim") cur.claimCount += 1;
      else cur.activeCount += 1;
      byOp.set(op, cur);
    }
    return Array.from(byOp.values()).sort((a, b) => a.operator.localeCompare(b.operator));
  }, [locks]);

  const handleExportLogs = async () => {
    const params = new URLSearchParams();
    if (logDir) params.set("log_dir", logDir);
    try {
      await apiDownload(`/api/export-logs-zip?${params}`, "rack-logs.zip");
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "Log export failed");
    }
  };

  const handleExportExcel = async () => {
    const params = new URLSearchParams();
    if (logDir) params.set("log_dir", logDir);
    setExcelExporting(true);
    try {
      await apiDownload(`/api/parser-export-excel?${params}`, "rack-parser-export.xlsx");
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "Excel export failed");
    } finally {
      setExcelExporting(false);
    }
  };

  return (
    <>
      {excelExporting && (
        <LinearProgress
          color="primary"
          sx={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            zIndex: (t) => t.zIndex.modal + 2,
            height: 3,
          }}
        />
      )}
      <AppBar
        position="sticky"
        elevation={0}
        aria-label="Application toolbar"
        sx={{ borderBottom: 1, borderColor: "divider", zIndex: (t) => t.zIndex.drawer + 1 }}
      >
        <MuiToolbar>
          {!sidebarOpen && (
            <Tooltip title="Expand menu">
              <IconButton
                edge="start"
                color="inherit"
                aria-label="open sidebar"
                onClick={onMenuToggle}
                sx={{ mr: 1 }}
              >
                <Menu size={24} />
              </IconButton>
            </Tooltip>
          )}

          <Text kind="body/bold/md">{title}</Text>

          <Box sx={{ flexGrow: 1, display: "flex", alignItems: "center", ml: 2 }}>
            <Box sx={{ width: "33%", minWidth: 220 }}>
              <ProjectSwitcher onSwitched={onClusterSwitched} />
            </Box>
          </Box>

          <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
            {loading && <CircularProgress size={18} color="primary" sx={{ mr: 1 }} />}

            <Tooltip title="Export logs (zip)">
              <IconButton color="inherit" onClick={handleExportLogs} size="small">
                <Download size={18} />
              </IconButton>
            </Tooltip>

            <Tooltip
              title={
                excelExporting
                  ? "Building Excel export (parser running)…"
                  : "Export parser results (Excel) — log root only; logs under failed/ are excluded"
              }
            >
              <span>
                <IconButton
                  color="inherit"
                  onClick={() => void handleExportExcel()}
                  size="small"
                  disabled={excelExporting}
                  aria-busy={excelExporting}
                  aria-label={excelExporting ? "Exporting Excel" : "Export parser results to Excel"}
                >
                  {excelExporting ? <CircularProgress color="inherit" size={18} /> : <FileSpreadsheet size={18} />}
                </IconButton>
              </span>
            </Tooltip>

            <Tooltip title="squeue">
              <IconButton color="inherit" onClick={() => setSqueueOpen(true)} size="small">
                <ListOrdered size={18} />
              </IconButton>
            </Tooltip>

            <Tooltip title="sinfo -R">
              <IconButton color="inherit" onClick={() => setSinfoOpen(true)} size="small">
                <Server size={18} />
              </IconButton>
            </Tooltip>

            <Tooltip title="Head node terminal">
              <IconButton color="inherit" onClick={() => setHeadTerminalOpen(true)} size="small">
                <Terminal size={18} />
              </IconButton>
            </Tooltip>

            <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />

            <Tooltip title="Refresh data">
              <IconButton color="inherit" onClick={onRefresh} disabled={loading} size="small">
                <RefreshCw size={18} />
              </IconButton>
            </Tooltip>

            <Tooltip title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}>
              <IconButton color="inherit" onClick={onToggleTheme} size="small">
                {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
              </IconButton>
            </Tooltip>

            <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />

            <ConnectionStatusIndicator
              backendConnected={backendConnected}
              clusterReachable={clusterReachable}
              execMode={execMode}
              lastUpdated={lastUpdated}
              onReconnect={onReconnect}
            />

            {activeUsers != null && activeUsers > 0 && (
              <Tooltip title="Who is doing what">
                <Box
                  component="span"
                  sx={{ marginRight: "4px", cursor: "pointer" }}
                  onClick={(e) => setUsersAnchor(e.currentTarget)}
                  aria-haspopup="true"
                  aria-expanded={Boolean(usersAnchor)}
                  role="button"
                >
                  <Tag color="gray" kind="solid" readOnly density="compact">
                    <Users size={14} style={{ marginRight: 4, verticalAlign: "middle" }} />
                    {activeUsers}
                  </Tag>
                </Box>
              </Tooltip>
            )}
            <Popover
              open={Boolean(usersAnchor)}
              anchorEl={usersAnchor}
              onClose={() => setUsersAnchor(null)}
              anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
              transformOrigin={{ vertical: "top", horizontal: "right" }}
              slotProps={{ paper: { sx: { p: 2, minWidth: 260, maxWidth: 360 } } }}
            >
              <Flex direction="col" gap="3">
                <Text kind="body/bold/sm">Active operators</Text>
                {operatorActivity.length === 0 ? (
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    No racks currently held.
                  </Text>
                ) : (
                  operatorActivity.map((entry) => (
                    <Box key={entry.operator}>
                      <Flex direction="row" gap="2" align="center">
                        <Text kind="body/bold/sm">
                          {entry.operator}
                          {entry.operator === me ? " (you)" : ""}
                        </Text>
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                          {entry.activeCount > 0 ? `${entry.activeCount} active` : ""}
                          {entry.activeCount > 0 && entry.claimCount > 0 ? " · " : ""}
                          {entry.claimCount > 0 ? `${entry.claimCount} claim${entry.claimCount > 1 ? "s" : ""}` : ""}
                        </Text>
                      </Flex>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                        {entry.racks.join(", ")}
                      </Text>
                    </Box>
                  ))
                )}
              </Flex>
            </Popover>

            {user && (
              <span style={{ marginRight: 4 }}>
                <Tag color="gray" kind="solid" readOnly density="compact">{user}</Tag>
              </span>
            )}

            <Tooltip title="Sign out">
              <IconButton color="inherit" onClick={logout} size="small">
                <LogOut size={18} />
              </IconButton>
            </Tooltip>
          </Box>
        </MuiToolbar>
      </AppBar>

      <SqueueDialog open={squeueOpen} onClose={() => setSqueueOpen(false)} logDir={logDir} />
      <SinfoDialog open={sinfoOpen} onClose={() => setSinfoOpen(false)} />
      {headTerminalOpen ? (
        <Suspense fallback={null}>
          <WebTerminalDialog
            open
            onClose={() => setHeadTerminalOpen(false)}
            target="headnode"
            label="Head Node"
            mode="headnode"
          />
        </Suspense>
      ) : null}
    </>
  );
}
