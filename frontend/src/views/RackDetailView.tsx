import { useState, useMemo, useEffect, useRef, useCallback } from "react";
import Box from "@mui/material/Box";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Button, Banner, Text } from "@nvidia/foundations-react-core";
import { ArrowLeft } from "lucide-react";
import { RackOverview } from "../components/rack/RackOverview";
import { NodeSelector, parseNodesFromRack, type NodeInfo } from "../components/rack/NodeSelector";
import { RackLastOutputDot } from "../components/rack/RackLastOutputDot";
import { ValidationTab } from "../components/rack/ValidationTab";
import { HardwareTab } from "../components/rack/HardwareTab";
import { NetworkingTab } from "../components/rack/NetworkingTab";
import { ServicesTab } from "../components/rack/ServicesTab";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import WebTerminalDialog from "../components/rack/WebTerminalDialog";
import { RackCommandProvider } from "../context/RackCommandContext";
import type { RackRow } from "../types/api";
import type { NvisNamingSchema } from "../types/naming";

interface RackDetailViewProps {
  rack: string;
  row: RackRow | undefined;
  comment: string;
  physicalRf0: Record<string, string>;
  naming?: NvisNamingSchema;
  logDir?: string;
  onBack: () => void;
}

export function RackDetailView({ rack, row, comment, physicalRf0, naming, logDir, onBack }: RackDetailViewProps) {
  const [tabIndex, setTabIndex] = useState(0);
  const [selectedNodes, setSelectedNodes] = useState<Set<string>>(new Set());
  const [parserOpen, setParserOpen] = useState(false);
  const [terminalNode, setTerminalNode] = useState("");

  const nodes = useMemo(() => {
    if (!row) return [];
    return parseNodesFromRack(rack, row.ct_down_unk, physicalRf0, 8, naming);
  }, [rack, row, physicalRf0, naming]);

  /** Default selection = all trays; re-seed when rack or the tray id set changes (not on every row poll with same ids). */
  const selectionSeedRef = useRef<string>("");
  useEffect(() => {
    if (nodes.length === 0) return;
    const seed = `${rack}:${nodes.map((n) => n.id).join(",")}`;
    if (selectionSeedRef.current === seed) return;
    selectionSeedRef.current = seed;
    setSelectedNodes(new Set(nodes.map((n) => n.id)));
  }, [rack, nodes]);

  const selectDownTraysForPowerReset = useCallback(
    (nodeList: NodeInfo[]) => {
      const downs = nodeList.filter((n) => n.status === "down").map((n) => n.id);
      if (downs.length === 0) {
        setSelectedNodes(new Set(nodeList.map((n) => n.id)));
      } else {
        setSelectedNodes(new Set(downs));
      }
    },
    [],
  );

  return (
    <RackCommandProvider rack={rack} logDir={logDir} jobId={row?.last_job}>
      <Box sx={{ maxWidth: 1200, mx: "auto", p: 4 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 3, flexWrap: "wrap" }}>
          <Button kind="tertiary" size="small" onClick={onBack}>
            <ArrowLeft size={18} style={{ marginRight: 4 }} />
            Back
          </Button>
          <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, minWidth: 0 }}>
            <Text kind="title/lg">{rack}</Text>
            <RackLastOutputDot />
          </Box>
        </Box>

        {row && <RackOverview row={row} comment={comment} />}

        <Box sx={{ mt: 3 }}>
          <Text kind="body/bold/md" style={{ marginBottom: 8, display: "block" }}>
            Nodes
          </Text>
          <NodeSelector
            nodes={nodes}
            selected={selectedNodes}
            onSelectionChange={setSelectedNodes}
            onOpenTerminal={(nodeId) => setTerminalNode(nodeId)}
          />
        </Box>

        <Box sx={{ mt: 3 }}>
          {selectedNodes.size === 0 && (
            <div style={{ marginBottom: 16 }}>
              <Banner status="info">
                Select one or more nodes above to enable actions.
              </Banner>
            </div>
          )}

          <Tabs
            value={tabIndex}
            onChange={(_, v) => setTabIndex(v)}
            sx={{ borderBottom: 1, borderColor: "divider", mb: 3 }}
          >
            <Tab label="Validation" />
            <Tab label="Hardware" />
            <Tab label="Networking" />
            <Tab label="Services" />
          </Tabs>

          {tabIndex === 0 && (
            <ValidationTab
              rack={rack}
              logDir={logDir}
              naming={naming}
              selectedNodes={selectedNodes}
              disabled={selectedNodes.size === 0}
              jobId={row?.last_job}
              onViewResults={() => setParserOpen(true)}
            />
          )}
          {tabIndex === 1 && (
            <HardwareTab
              rack={rack}
              logDir={logDir}
              naming={naming}
              selectedNodes={selectedNodes}
              disabled={selectedNodes.size === 0}
              onSelectPowerResetTargets={() => selectDownTraysForPowerReset(nodes)}
            />
          )}
          {tabIndex === 2 && (
            <NetworkingTab
              rack={rack}
              logDir={logDir}
              selectedNodes={selectedNodes}
              disabled={selectedNodes.size === 0}
            />
          )}
          {tabIndex === 3 && (
            <ServicesTab
              rack={rack}
              logDir={logDir}
              naming={naming}
              selectedNodes={selectedNodes}
              disabled={selectedNodes.size === 0}
            />
          )}
        </Box>
      </Box>

      <ParserResultsDialog
        open={parserOpen}
        onClose={() => setParserOpen(false)}
        rack={rack}
        jobId={row?.last_job !== "-" ? row?.last_job : undefined}
        logDir={logDir}
      />

      <WebTerminalDialog
        open={!!terminalNode}
        onClose={() => setTerminalNode("")}
        target={terminalNode}
        mode="node"
      />
    </RackCommandProvider>
  );
}
