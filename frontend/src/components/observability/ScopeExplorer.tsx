import { useMemo, useState, type CSSProperties, type ReactNode } from "react";
import Box from "@mui/material/Box";
import { SidePanel, Button, Flex, Text, StatusIndicator } from "@nvidia/foundations-react-core";
import { SlidersHorizontal } from "lucide-react";
import { ScopeTree } from "./ScopeTree";
import {
  CLUSTER_TOPOLOGY,
  allAvailableHostIds,
  selectionSummary,
  type ScopeCluster,
} from "./mockData";

/** Width of the scope SidePanel; content reserves this much room when open. */
const SCOPE_PANEL_WIDTH = 340;

/**
 * Top offset so the docked panel sits below the app chrome instead of behind it:
 * the sticky MUI AppBar/Toolbar (~64px) + the breadcrumb bar (~41px).
 */
const APP_HEADER_OFFSET = 105;

interface ScopeExplorerProps {
  /** Topology to explore; defaults to the mock cluster. Supply real data later. */
  topology?: ScopeCluster;
  /** Right-aligned status note shown above the content (e.g. live window). */
  statusNote?: string;
  /**
   * Content area renderer. Receives the selected host ids and a human summary so
   * each view can scope its data to the selection.
   */
  children: (selectedHosts: string[], summary: string) => ReactNode;
}

/**
 * Reusable layout for scope-driven views (Observability: Metrics / Logs /
 * Events). The cluster → rack → node selection tree lives in a right-docked KUI
 * SidePanel that is open by default and collapsible via the "Scope" toggle (or
 * the panel's close button). The content reserves room on the right so nothing
 * hides behind the panel while open, and reflows to full width when collapsed.
 */
export function ScopeExplorer({ topology = CLUSTER_TOPOLOGY, statusNote, children }: ScopeExplorerProps) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(allAvailableHostIds(topology)));
  const [scopeOpen, setScopeOpen] = useState(true);

  const selectedHosts = useMemo(() => Array.from(selected), [selected]);
  const summary = selectionSummary(topology, selected);

  // Forwarded to the SidePanelContent element (.nv-side-panel-content): sets the
  // panel width and offsets it below the app header so its heading/close button
  // are not hidden behind the sticky navbar.
  const panelStyle = {
    "--side-panel-width": `${SCOPE_PANEL_WIDTH}px`,
    top: APP_HEADER_OFFSET,
    height: `calc(100vh - ${APP_HEADER_OFFSET}px)`,
  } as CSSProperties;

  return (
    <Box>
      <Box
        sx={{
          transition: "padding-right 0.2s ease",
          pr: scopeOpen ? `${SCOPE_PANEL_WIDTH + 24}px` : 0,
        }}
      >
        <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 16 }}>
          <Text kind="body/bold/sm">{summary}</Text>
          <Flex align="center" gap="3">
            {statusNote && (
              <Flex align="center" gap="2">
                <StatusIndicator color="green" size="small" />
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  {statusNote}
                </Text>
              </Flex>
            )}
            <Button
              kind="secondary"
              size="small"
              onClick={() => setScopeOpen((open) => !open)}
              aria-expanded={scopeOpen}
            >
              <SlidersHorizontal size={16} />
              Scope
            </Button>
          </Flex>
        </Flex>

        {children(selectedHosts, summary)}
      </Box>

      <SidePanel
        bordered
        side="right"
        modal={false}
        closeOnClickOutside={false}
        open={scopeOpen}
        onOpenChange={setScopeOpen}
        slotHeading="Scope"
        style={panelStyle}
      >
        <ScopeTree topology={topology} selected={selected} onSelectedChange={setSelected} />
      </SidePanel>
    </Box>
  );
}
