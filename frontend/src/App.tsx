import { useState, useCallback } from "react";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { ThemeProvider, useThemeMode } from "./context/ThemeContext";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { AppLayout } from "./components/AppLayout";
import { Sidebar, type ViewId } from "./components/Sidebar";
import { Toolbar } from "./components/Toolbar";
import { DashboardView } from "./views/DashboardView";
import { RackDetailView } from "./views/RackDetailView";
import { MultirackView } from "./views/MultirackView";
import { SlurmView } from "./views/SlurmView";
import { ReportsView } from "./views/ReportsView";
import { SettingsView } from "./views/SettingsView";
import { LoginPage } from "./pages/LoginPage";
import { useRackStatus } from "./hooks/useRackStatus";

const SIDEBAR_STORAGE_KEY = "nvis-sidebar-open";

const VIEW_TITLES: Record<ViewId, string> = {
  dashboard: "Dashboard",
  multirack: "Multirack",
  slurm: "Slurm",
  reports: "Reports",
  settings: "Settings",
};

function getInitialSidebarState(): boolean {
  try {
    const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY);
    return stored !== "false";
  } catch {
    return true;
  }
}

function AppContent() {
  const { mode, toggleTheme } = useThemeMode();
  const [logDir, setLogDir] = useState("var/logs/");
  const [activeView, setActiveView] = useState<ViewId>("dashboard");
  const [sidebarOpen, setSidebarOpen] = useState(getInitialSidebarState);
  const [selectedRack, setSelectedRack] = useState<string | null>(null);

  const { data, loading, error, refresh, activeUsers } = useRackStatus(logDir);

  const handleSidebarToggle = useCallback(() => {
    setSidebarOpen((prev) => {
      const next = !prev;
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next));
      return next;
    });
  }, []);

  const handleNavigate = useCallback((view: ViewId) => {
    setActiveView(view);
    setSelectedRack(null);
  }, []);

  const handleRackSelect = useCallback((rack: string) => {
    setSelectedRack(rack);
  }, []);

  const handleBackToDashboard = useCallback(() => {
    setSelectedRack(null);
  }, []);

  const handleCommentChange = useCallback((rack: string, comment: string | null) => {
    if (!data) return;
    if (comment) {
      data.comments[rack] = comment;
    } else {
      delete data.comments[rack];
    }
    refresh();
  }, [data, refresh]);

  const toolbarTitle = selectedRack
    ? selectedRack
    : VIEW_TITLES[activeView];

  return (
    <AppLayout
      sidebar={
        <Sidebar
          open={sidebarOpen}
          onToggle={handleSidebarToggle}
          activeView={activeView}
          onNavigate={handleNavigate}
        />
      }
      toolbar={
        <Toolbar
          title={toolbarTitle}
          theme={mode}
          onToggleTheme={toggleTheme}
          onRefresh={refresh}
          loading={loading}
          sidebarOpen={sidebarOpen}
          onMenuToggle={handleSidebarToggle}
          activeUsers={activeUsers}
          logDir={logDir}
        />
      }
      backendMode={data?.native_workers?.mode}
    >
      {activeView === "dashboard" && !selectedRack && (
        <DashboardView
          data={data}
          loading={loading}
          error={error}
          onRackSelect={handleRackSelect}
          onCommentChange={handleCommentChange}
        />
      )}
      {activeView === "dashboard" && selectedRack && (
        <RackDetailView
          rack={selectedRack}
          row={data?.rows.find((r) => r.rack === selectedRack)}
          comment={data?.comments[selectedRack] || ""}
          physicalRf0={data?.physical_rf0 || {}}
          logDir={data?.log_dir}
          onBack={handleBackToDashboard}
        />
      )}
      {activeView === "multirack" && <MultirackView data={data} logDir={logDir} />}
      {activeView === "slurm" && <SlurmView />}
      {activeView === "reports" && <ReportsView />}
      {activeView === "settings" && (
        <SettingsView logDir={logDir} onLogDirChange={setLogDir} />
      )}
    </AppLayout>
  );
}

function AuthGate() {
  const { authenticated, loading } = useAuth();

  if (loading) {
    return (
      <Box sx={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", bgcolor: "#121212" }}>
        <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>Loading...</Text>
      </Box>
    );
  }

  if (!authenticated) {
    return <LoginPage />;
  }

  return <AppContent />;
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
    </ThemeProvider>
  );
}
