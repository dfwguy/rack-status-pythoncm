import { useState, useMemo } from "react";
import Box from "@mui/material/Box";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Button, Banner, Text } from "@nvidia/foundations-react-core";
import { ArrowLeft } from "lucide-react";
import { RackOverview } from "../components/rack/RackOverview";
import { NodeSelector, parseNodesFromRack } from "../components/rack/NodeSelector";
import { ValidationTab } from "../components/rack/ValidationTab";
import { HardwareTab } from "../components/rack/HardwareTab";
import { NetworkingTab } from "../components/rack/NetworkingTab";
import { ServicesTab } from "../components/rack/ServicesTab";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import type { RackRow } from "../types/api";

interface RackDetailViewProps {
  rack: string;
  row: RackRow | undefined;
  comment: string;
  physicalRf0: Record<string, string>;
  logDir?: string;
  onBack: () => void;
}

export function RackDetailView({ rack, row, comment, physicalRf0, logDir, onBack }: RackDetailViewProps) {
  const [tabIndex, setTabIndex] = useState(0);
  const [selectedNodes, setSelectedNodes] = useState<Set<string>>(new Set());
  const [parserOpen, setParserOpen] = useState(false);

  const nodes = useMemo(() => {
    if (!row) return [];
    return parseNodesFromRack(rack, row.ct_down_unk, physicalRf0);
  }, [rack, row, physicalRf0]);

  return (
    <Box sx={{ maxWidth: 1200, mx: "auto", p: 4 }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 3 }}>
        <Button
          kind="tertiary"
          size="small"
          onClick={onBack}
        >
          <ArrowLeft size={18} style={{ marginRight: 4 }} />
          Back
        </Button>
        <Text kind="title/lg">{rack}</Text>
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
            selectedNodes={selectedNodes}
            disabled={selectedNodes.size === 0}
            jobId={row?.last_job}
            onViewResults={() => setParserOpen(true)}
          />
        )}
        {tabIndex === 1 && (
          <HardwareTab rack={rack} selectedNodes={selectedNodes} disabled={selectedNodes.size === 0} />
        )}
        {tabIndex === 2 && (
          <NetworkingTab rack={rack} selectedNodes={selectedNodes} disabled={selectedNodes.size === 0} />
        )}
        {tabIndex === 3 && (
          <ServicesTab rack={rack} selectedNodes={selectedNodes} disabled={selectedNodes.size === 0} />
        )}
      </Box>

      <ParserResultsDialog
        open={parserOpen}
        onClose={() => setParserOpen(false)}
        rack={rack}
        jobId={row?.last_job !== "-" ? row?.last_job : undefined}
        logDir={logDir}
      />
    </Box>
  );
}
