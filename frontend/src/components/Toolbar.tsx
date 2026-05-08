import { useState } from "react";
import AppBar from "@mui/material/AppBar";
import MuiToolbar from "@mui/material/Toolbar";
import IconButton from "@mui/material/IconButton";
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import CircularProgress from "@mui/material/CircularProgress";
import LinearProgress from "@mui/material/LinearProgress";
import { Text, Tag } from "@nvidia/foundations-react-core";
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
} from "lucide-react";
import { SqueueDialog } from "./SqueueDialog";
import { SinfoDialog } from "./SinfoDialog";
import { useAuth } from "../context/AuthContext";

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
}: ToolbarProps) {
  const { user, logout } = useAuth();
  const [squeueOpen, setSqueueOpen] = useState(false);
  const [sinfoOpen, setSinfoOpen] = useState(false);
  const [excelExporting, setExcelExporting] = useState(false);

  const handleExportLogs = () => {
    const params = new URLSearchParams();
    if (logDir) params.set("log_dir", logDir);
    window.open(`/api/export-logs-zip?${params}`, "_blank");
  };

  const handleExportExcel = async () => {
    const params = new URLSearchParams();
    if (logDir) params.set("log_dir", logDir);
    const url = `/api/parser-export-excel?${params}`;
    setExcelExporting(true);
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      if (!res.ok) {
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
          const j = (await res.json()) as { error?: string };
          throw new Error(typeof j.error === "string" ? j.error : `HTTP ${res.status}`);
        }
        throw new Error(`HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const cd = res.headers.get("content-disposition");
      let name = "rack-parser-export.xlsx";
      const m = cd && /filename="([^"]+)"/.exec(cd);
      if (m) name = m[1];
      const a = document.createElement("a");
      const href = URL.createObjectURL(blob);
      a.href = href;
      a.download = name;
      a.click();
      URL.revokeObjectURL(href);
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

          <Text kind="body/bold/md" style={{ flexGrow: 1 }}>
            {title}
          </Text>

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

            {activeUsers != null && activeUsers > 0 && (
              <Tooltip title={`${activeUsers} active user(s)`}>
                <span style={{ marginRight: 4 }}>
                  <Tag color="gray" kind="solid" readOnly density="compact">
                    <Users size={14} style={{ marginRight: 4, verticalAlign: "middle" }} />
                    {activeUsers}
                  </Tag>
                </span>
              </Tooltip>
            )}

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
    </>
  );
}
