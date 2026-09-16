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
  ScrollText,
  Bell,
  Boxes,
  FolderKanban,
  CircuitBoard,
  Code2,
  ClipboardCheck,
} from "lucide-react";

export type ViewId =
  | "dashboard"
  | "nodes"
  | "singleRack"
  | "scheduler"
  | "infraEthernet"
  | "infraIbNvlink"
  | "infraStorage"
  | "obsMetrics"
  | "obsLogs"
  | "obsEvents"
  | "multirack"
  | "reports"
  | "analytics"
  | "postRunNodeReport"
  | "tools"
  | "workflows"
  | "terminal"
  | "projects"
  | "platformCatalog"
  | "toolRegistry"
  | "apiReference"
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
 * Domain navigation per the UX framework (docs/UX_FRAMEWORK.md, Sections 5 & 8.2).
 * Compute → Infrastructure → Observability → Tools → Analytics, macro to micro.
 * Several leaves are design previews (mock data) pending backend wiring.
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
    kind: "group",
    id: "grp-compute",
    label: "Compute",
    icon: <Cpu size={ICON_SIZE} />,
    leaves: [
      { id: "dashboard", label: "Cluster overview", icon: <LayoutDashboard size={ICON_SIZE} /> },
      { id: "nodes", label: "Nodes", icon: <CircuitBoard size={ICON_SIZE} /> },
      { id: "singleRack", label: "Racks", icon: <Server size={ICON_SIZE} /> },
      { id: "multirack", label: "Multirack operations", icon: <Layers size={ICON_SIZE} /> },
      { id: "scheduler", label: "Scheduler", icon: <CalendarClock size={ICON_SIZE} /> },
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
    ],
  },
  {
    kind: "group",
    id: "grp-analytics",
    label: "Analytics & Benchmarks",
    icon: <BarChart3 size={ICON_SIZE} />,
    leaves: [
      { id: "analytics", label: "Analytics", icon: <LineChart size={ICON_SIZE} /> },
      { id: "postRunNodeReport", label: "Validation Status", icon: <ClipboardCheck size={ICON_SIZE} /> },
      { id: "reports", label: "Reports", icon: <FileDown size={ICON_SIZE} /> },
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

/** All navigable leaves, flattened (used by the collapsed icon rail). */
const allLeaves: NavLeaf[] = navEntries.flatMap((entry) =>
  entry.kind === "group" ? entry.leaves : [entry.leaf]
);

/** Bottom-pinned platform leaf (a view). */
const settingsLeaf: NavLeaf = { id: "settings", label: "Settings", icon: <Settings size={ICON_SIZE} /> };

/** Project docs and source on GitLab (opens in a new tab from the sidebar). */
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
  const groupItems: VerticalNavBaseItem[] = navEntries.map((entry) => {
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
      // Points at the in-app, browsable API reference rather than the git repo. A link to a
      // repository is not documentation an operator can use: it needs a clone, a checkout and
      // a reader. The generated reference is the same information, current for *this*
      // deployment, and the repo is still one click away from inside that page.
      id: "documentation",
      slotIcon: <BookOpen size={ICON_SIZE} />,
      asChild: true,
      children: (
        <a
          href="#/platform/api"
          onClick={(e) => {
            e.preventDefault();
            onNavigate("apiReference");
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
  const leaves = allLeaves;

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
          <Tooltip title="Documentation — API reference" placement="right" arrow>
            <ListItemButton
              onClick={() => onNavigate("apiReference")}
              aria-label="Open the API reference"
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
    return clusterName || projectName || "Rack Status";
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
          <svg
            width="32"
            height="32"
            viewBox="0 0 24 24"
            xmlns="http://www.w3.org/2000/svg"
            style={{ flexShrink: 0 }}
          >
            <path
              fill="#76B900"
              d="M8.948 8.798v-1.43a6.7 6.7 0 0 1 .424-.018c3.922-.124 6.493 3.374 6.493 3.374s-2.774 3.851-5.75 3.851c-.398 0-.787-.062-1.158-.185v-4.346c1.528.185 1.837.857 2.747 2.385l2.04-1.714s-1.492-1.952-4-1.952a6.016 6.016 0 0 0-.796.035m0-4.735v2.138l.424-.027c5.45-.185 9.01 4.47 9.01 4.47s-4.08 4.964-8.33 4.964c-.37 0-.733-.035-1.095-.097v1.325c.3.035.61.062.91.062 3.957 0 6.82-2.023 9.593-4.408.459.371 2.34 1.263 2.73 1.652-2.633 2.208-8.772 3.984-12.253 3.984-.335 0-.653-.018-.971-.053v1.864H24V4.063zm0 10.326v1.131c-3.657-.654-4.673-4.46-4.673-4.46s1.758-1.944 4.673-2.262v1.237H8.94c-1.528-.186-2.73 1.245-2.73 1.245s.68 2.412 2.739 3.11M2.456 10.9s2.164-3.197 6.5-3.533V6.201C4.153 6.59 0 10.653 0 10.653s2.35 6.802 8.948 7.42v-1.237c-4.84-.6-6.492-5.936-6.492-5.936z"
            />
          </svg>
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
