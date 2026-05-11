import Drawer from "@mui/material/Drawer";
import Box from "@mui/material/Box";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import Divider from "@mui/material/Divider";
import Tooltip from "@mui/material/Tooltip";
import IconButton from "@mui/material/IconButton";
import { Text } from "@nvidia/foundations-react-core";
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
} from "lucide-react";

export type ViewId =
  | "dashboard"
  | "singleRack"
  | "multirack"
  | "slurm"
  | "reports"
  | "analytics"
  | "tools"
  | "settings";

export const DRAWER_WIDTH_OPEN = 240;
export const DRAWER_WIDTH_CLOSED = 64;

interface NavItem {
  id: ViewId;
  label: string;
  icon: React.ReactNode;
}

const mainNavItems: NavItem[] = [
  { id: "dashboard", label: "Dashboard", icon: <LayoutDashboard size={22} /> },
  { id: "singleRack", label: "Single Rack", icon: <Server size={22} /> },
  { id: "multirack", label: "Multirack", icon: <Layers size={22} /> },
  { id: "slurm", label: "Slurm", icon: <Terminal size={22} /> },
  { id: "reports", label: "Reports", icon: <FileDown size={22} /> },
  { id: "analytics", label: "Analytics", icon: <LineChart size={22} /> },
  { id: "tools", label: "Utility", icon: <Wrench size={22} /> },
];

const bottomNavItems: NavItem[] = [
  { id: "settings", label: "Settings", icon: <Settings size={22} /> },
];

/** Project docs and source on GitLab (opens in a new tab from the sidebar). */
export const NVIS_RACK_STATUS_DOC_URL = "https://gitlab-master.nvidia.com/nvis/nvis-rack-status";

interface ExternalNavItem {
  label: string;
  icon: React.ReactNode;
  href?: string;
}

const externalItems: ExternalNavItem[] = [
  { label: "Documentation", icon: <BookOpen size={22} />, href: NVIS_RACK_STATUS_DOC_URL },
  { label: "Help", icon: <HelpCircle size={22} /> },
];

interface SidebarProps {
  open: boolean;
  onToggle: () => void;
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
  /** When set (from server env / overrides), shown top-left instead of “Rack Status”. */
  projectName?: string;
}

function NavList({
  items,
  collapsed,
  activeView,
  onNavigate,
}: {
  items: NavItem[];
  collapsed: boolean;
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
}) {
  return (
    <List disablePadding>
      {items.map(({ id, label, icon }) => {
        const active = activeView === id;
        return (
          <ListItem key={id} disablePadding sx={{ display: "block" }}>
            <Tooltip title={collapsed ? label : ""} placement="right" arrow>
              <ListItemButton
                onClick={() => onNavigate(id)}
                sx={{
                  minHeight: 44,
                  px: 2,
                  justifyContent: collapsed ? "center" : "initial",
                  bgcolor: active ? "action.selected" : "transparent",
                  borderRight: active ? 3 : 0,
                  borderColor: "primary.main",
                  "&:hover": { bgcolor: "action.hover" },
                }}
              >
                <ListItemIcon
                  sx={{
                    minWidth: 0,
                    mr: collapsed ? 0 : 2,
                    justifyContent: "center",
                    color: active ? "primary.main" : "text.secondary",
                  }}
                >
                  {icon}
                </ListItemIcon>
                {!collapsed && (
                  <ListItemText
                    primary={label}
                    slotProps={{
                      primary: {
                        sx: {
                          fontSize: 14,
                          fontWeight: active ? 600 : 400,
                          color: active ? "text.primary" : "text.secondary",
                        },
                      },
                    }}
                  />
                )}
              </ListItemButton>
            </Tooltip>
          </ListItem>
        );
      })}
    </List>
  );
}

export function Sidebar({ open, onToggle, activeView, onNavigate, projectName }: SidebarProps) {
  const width = open ? DRAWER_WIDTH_OPEN : DRAWER_WIDTH_CLOSED;
  const brandLabel = projectName?.trim() ? projectName.trim() : "Rack Status";

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

      {/* Main navigation */}
      <Box sx={{ flexGrow: 1, pt: 1 }}>
        <NavList items={mainNavItems} collapsed={!open} activeView={activeView} onNavigate={onNavigate} />
      </Box>

      {/* Bottom navigation */}
      <Divider />
      <Box sx={{ pb: 1 }}>
        <NavList items={bottomNavItems} collapsed={!open} activeView={activeView} onNavigate={onNavigate} />
        <List disablePadding>
          {externalItems.map(({ label, icon, href }) => (
            <ListItem key={label} disablePadding sx={{ display: "block" }}>
              <Tooltip
                title={
                  !open ? label : href ? `${label} (opens in new tab)` : label
                }
                placement="right"
                arrow
              >
                <ListItemButton
                  {...(href
                    ? {
                        component: "a" as const,
                        href,
                        target: "_blank",
                        rel: "noopener noreferrer",
                      }
                    : {})}
                  sx={{
                    minHeight: 44,
                    px: 2,
                    justifyContent: !open ? "center" : "initial",
                    "&:hover": { bgcolor: "action.hover" },
                  }}
                >
                  <ListItemIcon
                    sx={{
                      minWidth: 0,
                      mr: !open ? 0 : 2,
                      justifyContent: "center",
                      color: "text.secondary",
                    }}
                  >
                    {icon}
                  </ListItemIcon>
                  {open && (
                    <ListItemText
                      primary={label}
                      slotProps={{
                        primary: {
                          sx: { fontSize: 14, color: "text.secondary" },
                        },
                      }}
                    />
                  )}
                </ListItemButton>
              </Tooltip>
            </ListItem>
          ))}
        </List>
      </Box>
    </Drawer>
  );
}
