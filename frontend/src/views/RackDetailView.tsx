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
import { useState, useMemo, useEffect, useRef, useCallback } from "react";
import Box from "@mui/material/Box";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Button, Banner, Text } from "@nvidia/foundations-react-core";
import { ArrowLeft } from "lucide-react";
import { RackOverview } from "../components/rack/RackOverview";
import { NodeSelector, type NodeInfo } from "../components/rack/NodeSelector";
import { parseNodesFromRack } from "../lib/parseNodesFromRack";
import { RackLastOutputDot } from "../components/rack/RackLastOutputDot";
import { RackPendingTags } from "../components/rack/RackPendingTags";
import { ValidationTab } from "../components/rack/ValidationTab";
import { HardwareTab } from "../components/rack/HardwareTab";
import { NetworkingTab } from "../components/rack/NetworkingTab";
import { ServicesTab } from "../components/rack/ServicesTab";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import WebTerminalDialog from "../components/rack/WebTerminalDialog";
import { RackCommandProvider } from "../context/RackCommandProvider";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { VisuallyHidden } from "../components/a11y/VisuallyHidden";
import { NodeIssueHistoryDialog } from "../components/rack/NodeIssueHistoryDialog";
import { getContent } from "../content";
import { useActiveCluster } from "../context/ActiveClusterContext";
import { fetchPostRunNodeReport } from "../services/nodeIssuesService";
import { fetchSlurmNodeStatus, type SlurmNodeStatus } from "../services/schedulerService";
import type { NodeIssue, NodeIssueRollup, NodeIssuesResponse, RackRow } from "../types/api";
import type { NvisNamingSchema } from "../types/naming";

const RACK_DETAIL_HELP = getContent("rackDetail");

interface RackDetailViewProps {
  rack: string;
  row: RackRow | undefined;
  comment: string;
  physicalRf0: Record<string, string>;
  naming?: NvisNamingSchema;
  logDir?: string;
  statusRefreshToken?: number | null;
  onBack: () => void;
}

export function RackDetailView({ rack, row, comment, physicalRf0, naming, logDir, statusRefreshToken, onBack }: RackDetailViewProps) {
  const [tabIndex, setTabIndex] = useState(0);
  const [selectedNodes, setSelectedNodes] = useState<Set<string>>(new Set());
  const [parserOpen, setParserOpen] = useState(false);
  const [terminalNode, setTerminalNode] = useState("");
  const [issueNode, setIssueNode] = useState("");
  const [issueData, setIssueData] = useState<NodeIssuesResponse | null>(null);
  const [slurmByNode, setSlurmByNode] = useState<Record<string, SlurmNodeStatus>>({});
  const [slurmLoading, setSlurmLoading] = useState(false);
  const [slurmError, setSlurmError] = useState("");
  const [slurmUpdatedAt, setSlurmUpdatedAt] = useState("");
  const { activeCluster, activePlatform } = useActiveCluster();
  const traysPerRack =
    activeCluster?.geometry?.trays_per_rack || activePlatform?.geometry?.trays_per_rack || 18;

  const nodes = useMemo(() => {
    if (!row) return [];
    return parseNodesFromRack(rack, row.ct_down_unk, physicalRf0, traysPerRack, naming);
  }, [rack, row, physicalRf0, naming, traysPerRack]);
  const nodeKey = useMemo(() => nodes.map((node) => node.id).join(","), [nodes]);

  const loadSlurmStatus = useCallback(async (signal?: AbortSignal) => {
    const nodeIds = nodeKey.split(",").filter(Boolean);
    if (nodeIds.length === 0) return;
    setSlurmLoading(true);
    setSlurmError("");
    try {
      const response = await fetchSlurmNodeStatus(nodeIds, signal);
      setSlurmByNode(Object.fromEntries(response.nodes.map((node) => [node.node, node])));
      setSlurmUpdatedAt(new Date(response.captured_at || Date.now()).toLocaleTimeString());
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setSlurmError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!signal?.aborted) setSlurmLoading(false);
    }
  }, [nodeKey]);

  useEffect(() => {
    const controller = new AbortController();
    void loadSlurmStatus(controller.signal);
    return () => {
      controller.abort();
    };
  }, [loadSlurmStatus, statusRefreshToken]);

  useEffect(() => setSlurmByNode({}), [nodeKey]);

  /** Default selection = all trays; re-seed when rack or the tray id set changes (not on every row poll with same ids). */
  const selectionSeedRef = useRef<string>("");
  useEffect(() => {
    if (nodes.length === 0) return;
    const seed = `${rack}:${nodes.map((n) => n.id).join(",")}`;
    if (selectionSeedRef.current === seed) return;
    selectionSeedRef.current = seed;
    setSelectedNodes(new Set(nodes.map((n) => n.id)));
  }, [rack, nodes]);

  useEffect(() => {
    let alive = true;
    const loadIssues = async (refresh = false) => {
      try {
        const report = await fetchPostRunNodeReport({ rack, refresh });
        const issues: NodeIssue[] = [];
        const rollups: Record<string, NodeIssueRollup> = {};
        for (const node of report.nodes) {
          const synthetic: NodeIssue[] = node.runs.filter((run) => run.attributed_failure).map((run) => ({
            issue_id: `run-${run.activity_id}-${run.job_id}-${node.node}`,
            node: node.node,
            rack: node.rack,
            category: run.category || "validation",
            code: "validation_failure",
            reason: run.reason || `${run.tool} job ${run.job_id} ended ${run.state}`,
            severity: "failure",
            source: run.tool,
            validation_run_id: run.activity_id,
            scheduler_ref: run.job_id,
            evidence: "",
            drained: false,
            slurm_reason: node.slurm_reason,
            drain_result: "participation finding",
            operator: "validation ledger",
            created_at: run.started_at,
          }));
          const combined = [...node.findings, ...synthetic];
          issues.push(...combined);
          const latest = combined.sort((a, b) => b.created_at.localeCompare(a.created_at))[0] || null;
          rollups[node.node] = {
            node: node.node,
            rack: node.rack,
            issue_count: node.failure_count,
            imported_count: 0,
            local_count: combined.length,
            drained: node.slurm_state.toLowerCase().startsWith("drain"),
            nccl: combined.some((issue) => issue.category === "network" || issue.reason.toLowerCase().includes("nccl")),
            maintenance: combined.some((issue) => issue.category === "maintenance"),
            latest,
          };
        }
        if (alive) setIssueData({
          ok: true,
          updated_at: report.generated_at,
          punchcard_synced_at: "",
          summary: {
            nodes_tracked: report.summary.nodes,
            nodes_with_issues: report.nodes.filter((node) => node.failure_count > 0).length,
            currently_flagged_drained: report.summary.drained_nodes,
            repeat_nodes: report.summary.flagged_nodes,
            local_findings: issues.length,
            hardware_findings: issues.filter((issue) => issue.category === "hardware").length,
            network_findings: issues.filter((issue) => issue.category === "network").length,
          },
          nodes: rollups,
          issues,
        });
      } catch {
        // Node actions remain usable if the optional history source is offline.
      }
    };
    void loadIssues(false);
    const timer = window.setInterval(() => void loadIssues(false), 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [rack]);

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
      <Box
        component="section"
        sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}
        aria-label={RACK_DETAIL_HELP ? `${RACK_DETAIL_HELP.aria.label}: ${rack}` : `Rack detail: ${rack}`}
        aria-describedby={RACK_DETAIL_HELP ? "help-desc-rackDetail" : undefined}
      >
        {RACK_DETAIL_HELP && (
          <VisuallyHidden id="help-desc-rackDetail">{RACK_DETAIL_HELP.aria.description}</VisuallyHidden>
        )}
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 3, flexWrap: "wrap" }}>
          <Button kind="secondary" size="small" onClick={onBack}>
            <ArrowLeft size={18} style={{ marginRight: 4 }} />
            Back
          </Button>
          <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, minWidth: 0 }}>
            <Text kind="title/lg">{rack}</Text>
            <RackLastOutputDot />
          </Box>
        </Box>

        <RackPendingTags rack={rack} />

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
            issueNodes={issueData?.nodes}
            onOpenIssues={(nodeId) => setIssueNode(nodeId)}
            slurmByNode={slurmByNode}
            slurmLoading={slurmLoading}
            slurmError={slurmError}
            slurmUpdatedAt={slurmUpdatedAt}
            onRefreshSlurm={() => void loadSlurmStatus()}
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
              naming={naming}
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


      <NodeIssueHistoryDialog
        open={!!issueNode}
        node={issueNode}
        rollup={issueData?.nodes?.[issueNode]}
        issues={issueData?.issues ?? []}
        punchcardSyncedAt={issueData?.punchcard_synced_at}
        onClose={() => setIssueNode("")}
      />
    </RackCommandProvider>
  );
}
