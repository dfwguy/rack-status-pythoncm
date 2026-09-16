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
import { useState, useCallback, useEffect, useRef, lazy, Suspense, type ReactNode } from "react";
import { HashRouter, Routes, Route, Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { useThemeMode } from "./context/ThemeContext";
import { ThemeProvider } from "./context/ThemeProvider";
import { useAuth } from "./context/AuthContext";
import { AuthProvider } from "./context/AuthProvider";
import { useAIChat } from "./context/AIChatContext";
import { AIChatProvider } from "./context/AIChatProvider";
import { AppLayout } from "./components/AppLayout";
import { Sidebar, type ViewId } from "./components/Sidebar";
import { Toolbar } from "./components/Toolbar";
import { AppBreadcrumbs } from "./components/AppBreadcrumbs";
import { ConnectionBanner } from "./components/ConnectionBanner";
import AIChatWidget from "./components/ai/AIChatWidget";
import { ClusterDashboardView } from "./views/ClusterDashboardView";
import { SingleRackView } from "./views/SingleRackView";
import { useClusterOverview } from "./hooks/useClusterOverview";
import { useClusterOverviewLive } from "./hooks/useClusterOverviewLive";
import { RackDetailView } from "./views/RackDetailView";
import { apiFetch } from "./lib/apiFetch";
// Lazy-loaded routes: keep the initial (index) bundle small by code-splitting each view. Heavy
// deps move into their own on-demand chunks (chart.js -> Analytics/Metrics, the large ToolsView
// -> Tools). ClusterDashboard + SingleRack stay eager for fast first paint.
const NodesView = lazy(() => import("./views/NodesView").then((m) => ({ default: m.NodesView })));
const MultirackView = lazy(() => import("./views/MultirackView").then((m) => ({ default: m.MultirackView })));
const SlurmView = lazy(() => import("./views/SlurmView").then((m) => ({ default: m.SlurmView })));
const ReportsView = lazy(() => import("./views/ReportsView").then((m) => ({ default: m.ReportsView })));
const AnalyticsView = lazy(() => import("./views/AnalyticsView").then((m) => ({ default: m.AnalyticsView })));
const PostRunNodeReportView = lazy(() => import("./views/PostRunNodeReportView").then((m) => ({ default: m.PostRunNodeReportView })));
const ToolsView = lazy(() => import("./views/ToolsView").then((m) => ({ default: m.ToolsView })));
const WorkflowsView = lazy(() => import("./views/WorkflowsView").then((m) => ({ default: m.WorkflowsView })));
const MetricsView = lazy(() => import("./views/MetricsView").then((m) => ({ default: m.MetricsView })));
const LogsView = lazy(() => import("./views/LogsView").then((m) => ({ default: m.LogsView })));
const EventsView = lazy(() => import("./views/EventsView").then((m) => ({ default: m.EventsView })));
const InfrastructureView = lazy(() => import("./views/InfrastructureView").then((m) => ({ default: m.InfrastructureView })));
const SettingsView = lazy(() => import("./views/SettingsView").then((m) => ({ default: m.SettingsView })));
const ProjectsView = lazy(() => import("./views/ProjectsView").then((m) => ({ default: m.ProjectsView })));
const PlatformCatalogView = lazy(() => import("./views/PlatformCatalogView").then((m) => ({ default: m.PlatformCatalogView })));
const ToolRegistryView = lazy(() => import("./views/ToolRegistryView").then((m) => ({ default: m.ToolRegistryView })));
const ApiReferenceView = lazy(() => import("./views/ApiReferenceView").then((m) => ({ default: m.ApiReferenceView })));
import { ActiveClusterProvider } from "./context/ActiveClusterProvider";
import { LoginPage } from "./pages/LoginPage";
const TerminalView = lazy(() => import("./views/TerminalView"));
const TerminalGridTabView = lazy(() => import("./views/TerminalGridTabView"));
const TerminalWorkspaceView = lazy(() => import("./views/TerminalWorkspaceView"));
import { useRackStatus } from "./hooks/useRackStatus";
import type { RackStatusResponse } from "./types/api";
import { RackCommandsStoreProvider } from "./context/RackCommandsStoreProvider";
import { RackLocksProvider } from "./context/RackLocksProvider";
import { HelpPanelProvider } from "./context/HelpPanelProvider";
import HelpPanel from "./components/help/HelpPanel";
import { DEFAULT_PATH, RACKS_PATH, rackDetailPath, pathToRoute, rackIdFromPath, viewIdToPath } from "./routes";
import { buildAIContext } from "./content";

const SIDEBAR_STORAGE_KEY = "nvis-sidebar-open";

const VIEW_TITLES: Record<ViewId, string> = {
  dashboard: "Cluster overview",
  nodes: "Nodes",
  singleRack: "Racks",
  scheduler: "Scheduler",
  infraEthernet: "Ethernet",
  infraIbNvlink: "IB / NVLink",
  infraStorage: "Storage",
  obsMetrics: "Metrics",
  obsLogs: "Logs",
  obsEvents: "Events",
  multirack: "Multirack operations",
  reports: "Reports",
  analytics: "Analytics",
  postRunNodeReport: "Validation Status",
  tools: "Operations & Scripts",
  workflows: "Workflows",
  terminal: "Terminal",
  projects: "Projects & Clusters",
  platformCatalog: "Platform Catalog",
  toolRegistry: "Tool Registry",
  apiReference: "API Reference",
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
function ViewSuspenseFallback() {
  return (
    <Box sx={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "40vh" }}>
      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
        Loading…
      </Text>
    </Box>
  );
}

function RackDetailRoute({
  data,
  refreshToken,
  onBack,
}: {
  data: RackStatusResponse | null;
  refreshToken: number | null;
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
      statusRefreshToken={refreshToken}
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

  const {
    data,
    loading,
    error,
    reachable: statusReachable,
    unreachableDetail: statusUnreachableDetail,
    backendConnected,
    lastUpdated: statusLastUpdated,
    refresh,
    retry: retryStatus,
    activeUsers,
  } = useRackStatus(logDir);
  const {
    data: clusterOverview,
    loading: clusterLoading,
    refreshing: clusterRefreshing,
    error: clusterError,
    lastUpdated: clusterLastUpdated,
    refresh: refreshCluster,
    retry: retryCluster,
  } = useClusterOverview(logDir);
  const {
    data: clusterLive,
    loading: clusterLiveLoading,
    error: clusterLiveError,
    retry: retryClusterLive,
  } = useClusterOverviewLive(logDir);

  const refreshAll = useCallback(async () => {
    await Promise.all([refresh(), refreshCluster(), retryClusterLive()]);
  }, [refresh, refreshCluster, retryClusterLive]);

  const [reconnecting, setReconnecting] = useState(false);
  const clusterUnreachable =
    statusReachable === false || clusterOverview?.cluster_reachable === false;
  const unreachableDetail =
    statusUnreachableDetail || clusterOverview?.cluster_unreachable_detail || null;

  const handleReconnect = useCallback(async () => {
    setReconnecting(true);
    try {
      await apiFetch("/api/cluster-health/reconnect", { method: "POST" });
    } catch {
      /* ignore; refreshAll below will reflect the new state */
    }
    try {
      await refreshAll();
    } finally {
      setReconnecting(false);
    }
  }, [refreshAll]);

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
    const rack = selectedRack || undefined;
    setPageContext({
      page: activeView,
      rack,
      additional_context: buildAIContext(activeView, rack),
    });
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

  /**
   * A comment was saved server-side; pull the status feed so the table shows it.
   *
   * This used to also write `data.comments[rack]` directly as an optimistic update, which could
   * never work: `data` is useRackStatus's own state and the hook exposes no setter, so mutating
   * it in place neither triggered a re-render nor survived the next fetch — it only risked the
   * table rendering from an object React believed unchanged. The refresh is what updates the UI.
   */
  const handleCommentChange = useCallback(() => {
    refresh();
  }, [refresh]);

  const toolbarTitle = selectedRack ? selectedRack : VIEW_TITLES[activeView];

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
          onRefresh={() => {
            void refreshAll();
          }}
          loading={loading || clusterLoading || clusterRefreshing}
          sidebarOpen={sidebarOpen}
          onMenuToggle={handleSidebarToggle}
          activeUsers={activeUsers}
          logDir={logDir}
          backendConnected={backendConnected}
          clusterReachable={!clusterUnreachable}
          execMode={data?.exec_mode ?? clusterOverview?.exec_mode ?? "ssh"}
          lastUpdated={statusLastUpdated ?? clusterLastUpdated}
          onReconnect={() => void handleReconnect()}
        />
      }
      breadcrumbs={<AppBreadcrumbs />}
      backendMode={data?.native_workers?.mode}
    >
      <RackCommandsStoreProvider>
        <ConnectionBanner
          unreachable={Boolean(clusterUnreachable)}
          detail={unreachableDetail}
          reconnecting={reconnecting}
          onReconnect={() => void handleReconnect()}
        />
        <Suspense fallback={<ViewSuspenseFallback />}>
        <Routes>
          <Route path="/" element={<Navigate to={DEFAULT_PATH} replace />} />
          <Route
            path="/compute/overview"
            element={
              <ClusterDashboardView
                data={clusterOverview}
                loading={clusterLoading}
                refreshing={clusterRefreshing}
                lastUpdated={clusterLastUpdated}
                error={clusterError}
                onRetry={retryCluster}
                onClusterMetadataSaved={refreshCluster}
                liveData={clusterLive}
                liveLoading={clusterLiveLoading}
                liveError={clusterLiveError}
                onLiveRetry={retryClusterLive}
              />
            }
          />
          <Route path="/compute/nodes" element={<NodesView data={data} logDir={logDir} />} />
          <Route
            path="/compute/racks"
            element={
              <SingleRackView
                data={data}
                loading={loading}
                error={error}
                onRetry={retryStatus}
                onRackSelect={handleRackSelect}
                onCommentChange={handleCommentChange}
              />
            }
          />
          <Route
            path="/compute/racks/:rackId"
            element={<RackDetailRoute data={data} refreshToken={statusLastUpdated} onBack={handleBackToDashboard} />}
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
          <Route path="/tools/terminal" element={<TerminalWorkspaceView logDir={logDir} />} />
          <Route path="/analytics/overview" element={<AnalyticsView logDir={logDir} rows={data?.rows ?? []} status={data} />} />
          <Route path="/analytics/post-run-nodes" element={<PostRunNodeReportView />} />
          <Route path="/analytics/reports" element={<ReportsView />} />
          <Route path="/platform/projects" element={<ProjectsView />} />
          <Route path="/platform/catalog" element={<PlatformCatalogView />} />
          <Route path="/platform/tools" element={<ToolRegistryView />} />
          <Route path="/platform/api" element={<ApiReferenceView />} />
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
        </Suspense>
      </RackCommandsStoreProvider>
      <AIChatWidget />
      <HelpPanel />
    </AppLayout>
  );
}

function AuthGate() {
  return (
    <RequireAuth>
      <ActiveClusterProvider>
        <AIChatProvider>
          <HelpPanelProvider>
            <RackLocksProvider>
              <AppContent />
            </RackLocksProvider>
          </HelpPanelProvider>
        </AIChatProvider>
      </ActiveClusterProvider>
    </RequireAuth>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
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

  return children;
}

export default function App() {
  if (window.location.pathname === "/terminal") {
    return (
      <ThemeProvider>
        <AuthProvider>
          <RequireAuth>
            <Suspense fallback={<ViewSuspenseFallback />}>
              <TerminalView />
            </Suspense>
          </RequireAuth>
        </AuthProvider>
      </ThemeProvider>
    );
  }

  if (window.location.pathname === "/terminal-grid") {
    return (
      <ThemeProvider>
        <AuthProvider>
          <RequireAuth>
            <Suspense fallback={<ViewSuspenseFallback />}>
              <TerminalGridTabView />
            </Suspense>
          </RequireAuth>
        </AuthProvider>
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
