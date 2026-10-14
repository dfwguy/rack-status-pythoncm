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
import type { ReactNode } from "react";
import Drawer from "@mui/material/Drawer";
import Box from "@mui/material/Box";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemIcon from "@mui/material/ListItemIcon";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import { Text, VerticalNav, type VerticalNavBaseItem } from "@nvidia/foundations-react-core";
import { GreenlightMark } from "./GreenlightMark";
import { viewIdToPath } from "../routes";
import { useOptionalHelpPanel } from "../context/HelpPanelContext";
import { useActiveCluster } from "../context/ActiveClusterContext";
import {
  LayoutDashboard,
  Layers,
  Server,
  Terminal,
  FileDown,
  Settings,
  BookOpen,
  HelpCircle,
  ChevronLeft,
  LineChart,
  Wrench,
  Workflow,
  Cpu,
  BarChart3,
  Network,
  Cable,
  Share2,
  HardDrive,
  Activity,
  CalendarClock,
  Gauge,
  Scale,
  ScrollText,
  Bell,
  Boxes,
  FolderKanban,
  CircuitBoard,
  Code2,
  ClipboardCheck,
  LayoutGrid,
  ShieldCheck,
  Users,
  UsersRound,
  Package,
  HardDriveDownload,
  Rocket,
  Truck,
} from "lucide-react";
import { useP2pEnabled } from "../features/p2p/hooks/useP2pEnabled";

export type ViewId =
  | "dashboard"
  | "nodes"
  | "singleRack"
  | "slurm"
  | "infraEthernet"
  | "infraIbNvlink"
  | "infraStorage"
  | "obsMetrics"
  | "obsLogs"
  | "obsEvents"
  | "obsAuditTrail"
  | "multirack"
  | "p2pArtifacts"
  | "p2pCatalog"
  | "p2pRosters"
  | "p2pCampaigns"
  | "p2pRollouts"
  | "p2pSettings"
  | "reports"
  | "analytics"
  | "postRunNodeReport"
  | "deploymentTracker"
  | "tools"
  | "workflows"
  | "terminal"
  | "projects"
  | "identityUsers"
  | "identityGroups"
  | "identityPolicies"
  | "platformCatalog"
  | "toolRegistry"
  | "apiReference"
  | "documentation"
  | "settings";

export const DRAWER_WIDTH_OPEN = 248;
export const DRAWER_WIDTH_CLOSED = 64;

/** A navigable destination that maps to a `ViewId` switched in App.tsx. */
interface NavLeaf {
  id: ViewId;
  label: string;
  icon: ReactNode;
}

/** A domain grouping in the framework (Compute, Infrastructure, Tools, ...). */
interface NavGroup {
  kind: "group";
  /** Group id, namespaced to avoid colliding with leaf ViewIds. */
  id: string;
  label: string;
  icon: ReactNode;
  leaves: NavLeaf[];
}

/** A top-level destination with no sub-items (e.g. Observability). */
interface NavStandalone {
  kind: "leaf";
  leaf: NavLeaf;
}

type NavEntry = NavGroup | NavStandalone;

const ICON_SIZE = 20;

/**
 * Domain navigation per the UX framework (docs/UX_FRAMEWORK.md, Sections 5 & 8.2):
 * Compute → Infrastructure → Observability → Analytics, macro to micro.
 *
 * **Tools leads, ahead of the macro-to-micro spine.** A deliberate departure from the
 * framework's own ordering, ported from the Nscale TX deployment where it was arrived at in
 * practice: the framework orders domains by how the product is *described* (the compute
 * hierarchy is the spine), but an operator mid-bring-up is not browsing the hierarchy — they
 * are reaching for a script, a workflow or a terminal, many times an hour. Ordering by reach
 * frequency rather than by conceptual hierarchy is what makes the rail feel right.
 */
const navEntries: NavEntry[] = [
  {
    kind: "group",
    id: "grp-tools",
    label: "Tools",
    icon: <Wrench size={ICON_SIZE} />,
    leaves: [
      { id: "tools", label: "Operations & Scripts", icon: <Wrench size={ICON_SIZE} /> },
      { id: "workflows", label: "Workflows", icon: <Workflow size={ICON_SIZE} /> },
      { id: "terminal", label: "Terminal", icon: <Terminal size={ICON_SIZE} /> },
    ],
  },
  {
    // Artifact distribution (NVIS-P2P). Single-tenant per cluster, so the group only
    // appears when the active cluster has the capability enabled — see useP2pEnabled and
    // the filtering in ExpandedNav/CollapsedRail. Operations-adjacent, hence high on the rail.
    kind: "group",
    id: "grp-p2p",
    label: "Artifact Distribution",
    icon: <Package size={ICON_SIZE} />,
    leaves: [
      { id: "p2pArtifacts", label: "Artifact Fleet", icon: <HardDriveDownload size={ICON_SIZE} /> },
      { id: "p2pCatalog", label: "Catalog", icon: <Boxes size={ICON_SIZE} /> },
      { id: "p2pRosters", label: "Rosters", icon: <Layers size={ICON_SIZE} /> },
      { id: "p2pCampaigns", label: "Campaigns", icon: <Rocket size={ICON_SIZE} /> },
      { id: "p2pRollouts", label: "Rollouts", icon: <Truck size={ICON_SIZE} /> },
      { id: "p2pSettings", label: "Dragonfly Settings", icon: <Settings size={ICON_SIZE} /> },
    ],
  },
  {
    kind: "group",
    id: "grp-compute",
    label: "Compute",
    icon: <Cpu size={ICON_SIZE} />,
    leaves: [
      { id: "dashboard", label: "Cluster overview", icon: <LayoutDashboard size={ICON_SIZE} /> },
      { id: "nodes", label: "Nodes", icon: <CircuitBoard size={ICON_SIZE} /> },
      { id: "singleRack", label: "Racks", icon: <Server size={ICON_SIZE} /> },
      { id: "multirack", label: "Multirack operations", icon: <Layers size={ICON_SIZE} /> },
      { id: "slurm", label: "Scheduler", icon: <CalendarClock size={ICON_SIZE} /> },
    ],
  },
  {
    kind: "group",
    id: "grp-infra",
    label: "Infrastructure",
    icon: <Network size={ICON_SIZE} />,
    leaves: [
      { id: "infraEthernet", label: "Ethernet", icon: <Cable size={ICON_SIZE} /> },
      { id: "infraIbNvlink", label: "IB / NVLink", icon: <Share2 size={ICON_SIZE} /> },
      { id: "infraStorage", label: "Storage", icon: <HardDrive size={ICON_SIZE} /> },
    ],
  },
  {
    kind: "group",
    id: "grp-observability",
    label: "Observability",
    icon: <Activity size={ICON_SIZE} />,
    leaves: [
      { id: "obsMetrics", label: "Metrics", icon: <Gauge size={ICON_SIZE} /> },
      { id: "obsLogs", label: "Logs", icon: <ScrollText size={ICON_SIZE} /> },
      { id: "obsEvents", label: "Events", icon: <Bell size={ICON_SIZE} /> },
      { id: "obsAuditTrail", label: "Audit Trail", icon: <ScrollText size={ICON_SIZE} /> },
    ],
  },
  {
    kind: "group",
    id: "grp-analytics",
    label: "Analytics & Benchmarks",
    icon: <BarChart3 size={ICON_SIZE} />,
    leaves: [
      { id: "analytics", label: "Analytics", icon: <LineChart size={ICON_SIZE} /> },
      { id: "postRunNodeReport", label: "Post Run Node Report", icon: <ClipboardCheck size={ICON_SIZE} /> },
      { id: "deploymentTracker", label: "Deployment Tracker", icon: <LayoutGrid size={ICON_SIZE} /> },
      { id: "reports", label: "Reports", icon: <FileDown size={ICON_SIZE} /> },
    ],
  },
  {
    kind: "group",
    id: "grp-identity",
    label: "Identity",
    icon: <ShieldCheck size={ICON_SIZE} />,
    leaves: [
      { id: "identityUsers", label: "Users", icon: <Users size={ICON_SIZE} /> },
      { id: "identityGroups", label: "Groups", icon: <UsersRound size={ICON_SIZE} /> },
      { id: "identityPolicies", label: "Policies", icon: <Scale size={ICON_SIZE} /> },
    ],
  },
  {
    kind: "group",
    id: "grp-platform",
    label: "Platform",
    icon: <Boxes size={ICON_SIZE} />,
    leaves: [
      { id: "projects", label: "Projects & Clusters", icon: <FolderKanban size={ICON_SIZE} /> },
      { id: "platformCatalog", label: "Platform Catalog", icon: <CircuitBoard size={ICON_SIZE} /> },
      { id: "toolRegistry", label: "Tool Registry", icon: <Wrench size={ICON_SIZE} /> },
      { id: "apiReference", label: "API Reference", icon: <Code2 size={ICON_SIZE} /> },
    ],
  },
];

/**
 * Nav entries visible for the current context. Some groups are gated on a per-cluster
 * capability — Artifact Distribution (NVIS-P2P) is single-tenant per cluster — so the rail
 * shows them only when the active cluster has that capability enabled.
 */
function useVisibleNavEntries(): NavEntry[] {
  const p2pEnabled = useP2pEnabled();
  return navEntries.filter(
    (entry) => !(entry.kind === "group" && entry.id === "grp-p2p") || p2pEnabled,
  );
}

/** Visible navigable leaves, flattened (used by the collapsed icon rail). */
function useVisibleLeaves(): NavLeaf[] {
  return useVisibleNavEntries().flatMap((entry) =>
    entry.kind === "group" ? entry.leaves : [entry.leaf],
  );
}

/** Bottom-pinned platform leaf (a view). */
const settingsLeaf: NavLeaf = { id: "settings", label: "Settings", icon: <Settings size={ICON_SIZE} /> };

/**
 * The project on GitLab.
 *
 * **Not linked from the UI any more, deliberately.** Every in-app route to documentation now
 * points at what this deployment serves at `/docs/`: an operator reaches Greenlight through an
 * SSH tunnel on a customer cluster and frequently cannot reach gitlab-master at all, so a link
 * there was broken exactly where it was needed most — and it told an external user nothing they
 * could open. Kept as the canonical repository reference for a bug report or a commit message.
 */
export const NVIS_RACK_STATUS_DOC_URL = "https://gitlab-master.nvidia.com/nvis/nvis-rack-status";

interface SidebarProps {
  open: boolean;
  onToggle: () => void;
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
}

/**
 * A leaf rendered as an anchor so KUI's `VerticalNavItem` (an `<a>`) styles it
 * natively via `asChild`. Navigation is state-based (no router), so we prevent
 * the default and call `onNavigate`.
 */
function navLeafItem(leaf: NavLeaf, activeView: ViewId, onNavigate: (view: ViewId) => void): VerticalNavBaseItem {
  return {
    id: leaf.id,
    active: activeView === leaf.id,
    asChild: true,
    children: (
      <a
        href={`#${viewIdToPath(leaf.id)}`}
        onClick={(e) => {
          e.preventDefault();
          onNavigate(leaf.id);
        }}
      >
        {leaf.label}
      </a>
    ),
  };
}

/** Expanded navigation: collapsible KUI domain groups + bottom platform items. */
function ExpandedNav({
  activeView,
  onNavigate,
  onOpenHelp,
}: {
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
  onOpenHelp: () => void;
}) {
  const groupItems: VerticalNavBaseItem[] = useVisibleNavEntries().map((entry) => {
    if (entry.kind === "leaf") {
      const { leaf } = entry;
      return {
        id: leaf.id,
        active: activeView === leaf.id,
        slotIcon: leaf.icon,
        asChild: true,
        children: (
          <a
            href={`#${viewIdToPath(leaf.id)}`}
            onClick={(e) => {
              e.preventDefault();
              onNavigate(leaf.id);
            }}
          >
            {leaf.label}
          </a>
        ),
      };
    }
    return {
      id: entry.id,
      children: entry.label,
      slotIcon: entry.icon,
      defaultOpen: false,
      subItems: entry.leaves.map((leaf) => navLeafItem(leaf, activeView, onNavigate)),
    };
  });

  const bottomItems: VerticalNavBaseItem[] = [
    {
      id: settingsLeaf.id,
      active: activeView === settingsLeaf.id,
      slotIcon: settingsLeaf.icon,
      asChild: true,
      children: (
        <a
          href={`#${viewIdToPath(settingsLeaf.id)}`}
          onClick={(e) => {
            e.preventDefault();
            onNavigate(settingsLeaf.id);
          }}
        >
          {settingsLeaf.label}
        </a>
      ),
    },
    {
      // The documentation site this deployment serves at /docs/, not a link to the git repo. A
      // repository is not documentation an operator can use — it needs a clone, a checkout and
      // a reader — and an internet docs site is unreachable from a customer cluster behind an
      // SSH tunnel. Serving it from the app means the documentation always matches the code
      // running here.
      //
      // An in-app destination, not `<a href="/docs/" target="_blank">`, which is what this was.
      //
      // Two reasons it had to change. The desktop shell has no browser tab to open, so
      // `_blank` walked the operator out of the app entirely. And `/docs/` is a *server* route
      // behind the authorization gate: a plain navigation skips `apiFetch`, so it never gets
      // the transparent refresh that keeps the rest of the app signed in, and once the hour-long
      // access cookie expired the link showed the login screen instead of the documentation.
      //
      // `DocumentationView` renews the session first and then frames the same site, so the
      // credential is fresh before anything asks for it. "Open in a new window" is still there,
      // as a choice rather than the only way in.
      id: "documentation",
      slotIcon: <BookOpen size={ICON_SIZE} />,
      active: activeView === "documentation",
      asChild: true,
      children: (
        <a
          href="/documentation"
          onClick={(e) => {
            e.preventDefault();
            onNavigate("documentation");
          }}
        >
          Documentation
        </a>
      ),
    },
    {
      id: "help",
      slotIcon: <HelpCircle size={ICON_SIZE} />,
      asChild: true,
      children: (
        <a
          href="#help"
          onClick={(e) => {
            e.preventDefault();
            onOpenHelp();
          }}
        >
          Help
        </a>
      ),
    },
  ];

  return (
    <>
      <Box component="nav" aria-label="Primary navigation" sx={{ flexGrow: 1, overflowY: "auto", pt: 1 }}>
        <VerticalNav items={groupItems} />
      </Box>
      <Divider />
      <Box component="nav" aria-label="Settings, documentation, and help" sx={{ py: 1 }}>
        <VerticalNav items={bottomItems} />
      </Box>
    </>
  );
}

/**
 * Collapsed rail: icon-only destinations with tooltips. KUI's grouped VerticalNav
 * does not render as a 64px icon rail, so the rail uses a flat MUI icon list
 * (the Drawer shell is MUI as well). Clicking still navigates immediately.
 */
function CollapsedRail({
  activeView,
  onNavigate,
  onOpenHelp,
}: {
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
  onOpenHelp: () => void;
}) {
  const leaves = useVisibleLeaves();

  return (
    <Box sx={{ flexGrow: 1, pt: 1 }}>
      <List disablePadding>
        {leaves.map((leaf) => {
          const active = activeView === leaf.id;
          return (
            <ListItem key={leaf.id} disablePadding sx={{ display: "block" }}>
              <Tooltip title={leaf.label} placement="right" arrow>
                <ListItemButton
                  onClick={() => onNavigate(leaf.id)}
                  sx={{
                    minHeight: 44,
                    px: 0,
                    justifyContent: "center",
                    bgcolor: active ? "action.selected" : "transparent",
                    borderRight: active ? 3 : 0,
                    borderColor: "primary.main",
                    "&:hover": { bgcolor: "action.hover" },
                  }}
                >
                  <ListItemIcon
                    sx={{
                      minWidth: 0,
                      justifyContent: "center",
                      color: active ? "primary.main" : "text.secondary",
                    }}
                  >
                    {leaf.icon}
                  </ListItemIcon>
                </ListItemButton>
              </Tooltip>
            </ListItem>
          );
        })}
      </List>
      <Divider sx={{ my: 1 }} />
      <List disablePadding>
        {[settingsLeaf].map((leaf) => {
          const active = activeView === leaf.id;
          return (
            <ListItem key={leaf.id} disablePadding sx={{ display: "block" }}>
              <Tooltip title={leaf.label} placement="right" arrow>
                <ListItemButton
                  onClick={() => onNavigate(leaf.id)}
                  sx={{
                    minHeight: 44,
                    px: 0,
                    justifyContent: "center",
                    bgcolor: active ? "action.selected" : "transparent",
                    "&:hover": { bgcolor: "action.hover" },
                  }}
                >
                  <ListItemIcon
                    sx={{ minWidth: 0, justifyContent: "center", color: active ? "primary.main" : "text.secondary" }}
                  >
                    {leaf.icon}
                  </ListItemIcon>
                </ListItemButton>
              </Tooltip>
            </ListItem>
          );
        })}
        <ListItem disablePadding sx={{ display: "block" }}>
          <Tooltip title="Help" placement="right" arrow>
            <ListItemButton
              onClick={onOpenHelp}
              aria-label="Open help"
              sx={{ minHeight: 44, px: 0, justifyContent: "center", "&:hover": { bgcolor: "action.hover" } }}
            >
              <ListItemIcon sx={{ minWidth: 0, justifyContent: "center", color: "text.secondary" }}>
                <HelpCircle size={ICON_SIZE} />
              </ListItemIcon>
            </ListItemButton>
          </Tooltip>
        </ListItem>
        <ListItem disablePadding sx={{ display: "block" }}>
          {/* Same destination as the expanded rail's Documentation entry — the site this
              deployment serves at /docs/. A component, not onNavigate, because it leaves the
              SPA. */}
          <Tooltip title="Documentation" placement="right" arrow>
            <ListItemButton
              component="a"
              href="/docs/"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open the documentation"
              sx={{ minHeight: 44, px: 0, justifyContent: "center", "&:hover": { bgcolor: "action.hover" } }}
            >
              <ListItemIcon sx={{ minWidth: 0, justifyContent: "center", color: "text.secondary" }}>
                <BookOpen size={ICON_SIZE} />
              </ListItemIcon>
            </ListItemButton>
          </Tooltip>
        </ListItem>
      </List>
    </Box>
  );
}

export function Sidebar({ open, onToggle, activeView, onNavigate }: SidebarProps) {
  const width = open ? DRAWER_WIDTH_OPEN : DRAWER_WIDTH_CLOSED;
  const { projects, activeProject, activeCluster } = useActiveCluster();
  // Match ProjectSwitcher: multi-project → "Project / Cluster", else cluster (or project) name.
  const brandLabel = (() => {
    const projectName = activeProject?.name?.trim() || "";
    const clusterName = activeCluster?.name?.trim() || "";
    if (projectName && clusterName) {
      return projects.length > 1 ? `${projectName} / ${clusterName}` : clusterName;
    }
    return clusterName || projectName || "Greenlight";
  })();
  const helpPanel = useOptionalHelpPanel();
  const openHelp = () => helpPanel?.openHelp();

  return (
    <Drawer
      variant="permanent"
      sx={{
        width,
        flexShrink: 0,
        "& .MuiDrawer-paper": {
          width,
          overflowX: "hidden",
          transition: "width 225ms cubic-bezier(0.4, 0, 0.6, 1)",
        },
      }}
    >
      {/* Brand row */}
      <Box
        sx={(theme) => ({
          display: "flex",
          alignItems: "center",
          gap: 1,
          px: open ? 2 : 0,
          justifyContent: open ? "space-between" : "center",
          ...theme.mixins.toolbar,
        })}
      >
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, minWidth: 0, flex: open ? 1 : undefined }}>
          <GreenlightMark size={32} />
          {open && (
            <Text
              key={brandLabel}
              kind="body/bold/md"
              title={brandLabel}
              style={{
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                minWidth: 0,
              }}
            >
              {brandLabel}
            </Text>
          )}
        </Box>
        {open && (
          <IconButton size="small" onClick={onToggle} sx={{ color: "text.secondary" }}>
            <ChevronLeft size={22} />
          </IconButton>
        )}
      </Box>

      <Divider />

      {open ? (
        <ExpandedNav activeView={activeView} onNavigate={onNavigate} onOpenHelp={openHelp} />
      ) : (
        <CollapsedRail activeView={activeView} onNavigate={onNavigate} onOpenHelp={openHelp} />
      )}
    </Drawer>
  );
}
