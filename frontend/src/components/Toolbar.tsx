import { useState } from "react";
import AppBar from "@mui/material/AppBar";
import MuiToolbar from "@mui/material/Toolbar";
import IconButton from "@mui/material/IconButton";
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import CircularProgress from "@mui/material/CircularProgress";
import { Text, Tag } from "@nvidia/foundations-react-core";
import {
  Sun,
  Moon,
  RefreshCw,
  Menu,
  Download,
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

  const handleExportLogs = () => {
    const params = new URLSearchParams();
    if (logDir) params.set("log_dir", logDir);
    window.open(`/api/export-logs-zip?${params}`, "_blank");
  };

  return (
    <>
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

            <Tooltip title="Export Logs (zip)">
              <IconButton color="inherit" onClick={handleExportLogs} size="small">
                <Download size={18} />
              </IconButton>
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

      <SqueueDialog open={squeueOpen} onClose={() => setSqueueOpen(false)} />
      <SinfoDialog open={sinfoOpen} onClose={() => setSinfoOpen(false)} />
    </>
  );
}
