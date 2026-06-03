import { useState, useCallback, useEffect, useRef } from "react";
import { HashRouter, Routes, Route, Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { ThemeProvider, useThemeMode } from "./context/ThemeContext";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { AIChatProvider, useAIChat } from "./context/AIChatContext";
import { AppLayout } from "./components/AppLayout";
import { Sidebar, type ViewId } from "./components/Sidebar";
import { Toolbar } from "./components/Toolbar";
import { AppBreadcrumbs } from "./components/AppBreadcrumbs";
import AIChatWidget from "./components/ai/AIChatWidget";
import { ClusterDashboardView } from "./views/ClusterDashboardView";
import { SingleRackView } from "./views/SingleRackView";
import { useClusterOverview } from "./hooks/useClusterOverview";
import { RackDetailView } from "./views/RackDetailView";
import { MultirackView } from "./views/MultirackView";
import { SlurmView } from "./views/SlurmView";
import { ReportsView } from "./views/ReportsView";
import { AnalyticsView } from "./views/AnalyticsView";
import { ToolsView } from "./views/ToolsView";
import { WorkflowsView } from "./views/WorkflowsView";
import { MetricsView } from "./views/MetricsView";
import { LogsView } from "./views/LogsView";
import { EventsView } from "./views/EventsView";
import { InfrastructureView } from "./views/InfrastructureView";
import { TerminalPlaceholderView } from "./views/TerminalPlaceholderView";
import { SettingsView } from "./views/SettingsView";
import { LoginPage } from "./pages/LoginPage";
import TerminalView from "./views/TerminalView";
import { useRackStatus } from "./hooks/useRackStatus";
import type { RackStatusResponse } from "./types/api";
import { RackCommandsStoreProvider } from "./context/RackCommandsStoreContext";
import { RackLocksProvider } from "./context/RackLocksContext";
import { DEFAULT_PATH, RACKS_PATH, rackDetailPath, pathToRoute, rackIdFromPath, viewIdToPath } from "./routes";

const SIDEBAR_STORAGE_KEY = "nvis-sidebar-open";

const VIEW_TITLES: Record<ViewId, string> = {
  dashboard: "Cluster overview",
  singleRack: "Racks",
  slurm: "Scheduler",
  infraEthernet: "Ethernet",
  infraIbNvlink: "IB / NVLink",
  infraStorage: "Storage",
  obsMetrics: "Metrics",
  obsLogs: "Logs",
  obsEvents: "Events",
  multirack: "Multirack operations",
  reports: "Reports",
  analytics: "Analytics",
  tools: "Operations & Scripts",
  workflows: "Workflows",
  terminal: "Terminal",
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

/**
 * Rack detail route element. Reads `:rackId` from the URL and resolves the row
 * from the shared rack-status data (kept in `AppContent` so it persists across
 * navigation). Keeps `RackDetailView`'s existing prop contract.
 */
function RackDetailRoute({
  data,
  onBack,
}: {
  data: RackStatusResponse | null;
  onBack: () => void;
}) {
  const { rackId } = useParams<{ rackId: string }>();
  const rack = rackId ? decodeURIComponent(rackId) : "";
  const row =
    data?.rows.find((r) => r.rack === rack) ??
    data?.rows.find((r) => r.rack.toLowerCase() === rack.toLowerCase());

  return (
    <RackDetailView
      rack={rack}
      row={row}
      comment={data?.comments[rack] || ""}
      physicalRf0={data?.physical_rf0 || {}}
      naming={data?.naming}
      logDir={data?.log_dir}
      onBack={onBack}
    />
  );
}

function AppContent() {
  const { mode, toggleTheme } = useThemeMode();
  const location = useLocation();
  const navigate = useNavigate();
  /** Empty = omit log_dir on /api/status so the server uses RACK_STATUS_LOG_DIR (or app default). */
  const [logDir, setLogDir] = useState("");
  const logDirSeededRef = useRef(false);
  const [sidebarOpen, setSidebarOpen] = useState(getInitialSidebarState);

  /** Active view + selected rack are derived from the URL (refresh-safe). */
  const activeView: ViewId = pathToRoute(location.pathname)?.viewId ?? "dashboard";
  const selectedRack = rackIdFromPath(location.pathname);

  const { data, loading, error, refresh, activeUsers } = useRackStatus(logDir);
  const {
    data: clusterOverview,
    loading: clusterLoading,
    error: clusterError,
    refresh: refreshCluster,
  } = useClusterOverview(logDir);

  const refreshAll = useCallback(async () => {
    await Promise.all([refresh(), refreshCluster()]);
  }, [refresh, refreshCluster]);

  useEffect(() => {
    if (!data?.log_dir || logDirSeededRef.current) return;
    setLogDir((current) => {
      if (current !== "") return current;
      logDirSeededRef.current = true;
      return data.log_dir;
    });
  }, [data?.log_dir]);

  const handleSidebarToggle = useCallback(() => {
    setSidebarOpen((prev) => {
      const next = !prev;
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next));
      return next;
    });
  }, []);

  const { setPageContext } = useAIChat();

  const handleNavigate = useCallback(
    (view: ViewId) => {
      navigate(viewIdToPath(view));
    },
    [navigate]
  );

  useEffect(() => {
    setPageContext({ page: activeView, rack: selectedRack || undefined });
  }, [activeView, selectedRack, setPageContext]);

  const handleRackSelect = useCallback(
    (rack: string) => {
      navigate(rackDetailPath(rack));
    },
    [navigate]
  );

  const handleBackToDashboard = useCallback(() => {
    navigate(RACKS_PATH);
  }, [navigate]);

  const handleCommentChange = useCallback((rack: string, comment: string | null) => {
    if (!data) return;
    if (comment) {
      data.comments[rack] = comment;
    } else {
      delete data.comments[rack];
    }
    refresh();
  }, [data, refresh]);

  const toolbarTitle = selectedRack ? selectedRack : VIEW_TITLES[activeView];

  return (
    <AppLayout
      sidebar={
        <Sidebar
          open={sidebarOpen}
          onToggle={handleSidebarToggle}
          activeView={activeView}
          onNavigate={handleNavigate}
          projectName={data?.project_name}
        />
      }
      toolbar={
        <Toolbar
          title={toolbarTitle}
          theme={mode}
          onToggleTheme={toggleTheme}
          onRefresh={() => {
            void refreshAll();
          }}
          loading={loading || clusterLoading}
          sidebarOpen={sidebarOpen}
          onMenuToggle={handleSidebarToggle}
          activeUsers={activeUsers}
          logDir={logDir}
        />
      }
      breadcrumbs={<AppBreadcrumbs />}
      backendMode={data?.native_workers?.mode}
    >
      <RackCommandsStoreProvider>
        <Routes>
          <Route path="/" element={<Navigate to={DEFAULT_PATH} replace />} />
          <Route
            path="/compute/overview"
            element={
              <ClusterDashboardView
                data={clusterOverview}
                loading={clusterLoading}
                error={clusterError}
                onClusterMetadataSaved={refreshCluster}
              />
            }
          />
          <Route
            path="/compute/racks"
            element={
              <SingleRackView
                data={data}
                loading={loading}
                error={error}
                onRackSelect={handleRackSelect}
                onCommentChange={handleCommentChange}
              />
            }
          />
          <Route
            path="/compute/racks/:rackId"
            element={<RackDetailRoute data={data} onBack={handleBackToDashboard} />}
          />
          <Route path="/compute/multirack" element={<MultirackView data={data} logDir={logDir} />} />
          <Route path="/compute/scheduler" element={<SlurmView logDir={logDir} />} />
          <Route path="/infrastructure/ethernet" element={<InfrastructureView segment="ethernet" />} />
          <Route path="/infrastructure/ib-nvlink" element={<InfrastructureView segment="ib-nvlink" />} />
          <Route path="/infrastructure/storage" element={<InfrastructureView segment="storage" />} />
          <Route path="/observability/metrics" element={<MetricsView />} />
          <Route path="/observability/logs" element={<LogsView />} />
          <Route path="/observability/events" element={<EventsView />} />
          <Route path="/tools/operations" element={<ToolsView logDir={logDir} />} />
          <Route path="/tools/workflows" element={<WorkflowsView rackData={data} />} />
          <Route path="/tools/terminal" element={<TerminalPlaceholderView />} />
          <Route path="/analytics/overview" element={<AnalyticsView logDir={logDir} rows={data?.rows ?? []} />} />
          <Route path="/analytics/reports" element={<ReportsView />} />
          <Route
            path="/settings"
            element={
              <SettingsView
                logDir={logDir}
                onLogDirChange={setLogDir}
                onDashboardEnvSaved={refreshAll}
              />
            }
          />
          <Route path="*" element={<Navigate to={DEFAULT_PATH} replace />} />
        </Routes>
      </RackCommandsStoreProvider>
      <AIChatWidget />
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

  return (
    <AIChatProvider>
      <RackLocksProvider>
        <AppContent />
      </RackLocksProvider>
    </AIChatProvider>
  );
}

export default function App() {
  if (window.location.pathname === "/terminal") {
    return (
      <ThemeProvider>
        <TerminalView />
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider>
      <HashRouter>
        <AuthProvider>
          <AuthGate />
        </AuthProvider>
      </HashRouter>
    </ThemeProvider>
  );
}
