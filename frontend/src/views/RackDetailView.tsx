import { useState, useMemo, useEffect, useRef, useCallback } from "react";
import Box from "@mui/material/Box";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import TextField from "@mui/material/TextField";
import { Button, Banner, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { ArrowLeft, Lock } from "lucide-react";
import { RackOverview } from "../components/rack/RackOverview";
import { NodeSelector, parseNodesFromRack, type NodeInfo } from "../components/rack/NodeSelector";
import { RackLastOutputDot } from "../components/rack/RackLastOutputDot";
import { ValidationTab } from "../components/rack/ValidationTab";
import { HardwareTab } from "../components/rack/HardwareTab";
import { NetworkingTab } from "../components/rack/NetworkingTab";
import { ServicesTab } from "../components/rack/ServicesTab";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import { RackCommandProvider } from "../context/RackCommandContext";
import { useRackLocksContext } from "../context/RackLocksContext";
import { apiFetch } from "../lib/apiFetch";
import { getOperatorName, isAutoClaimEnabled } from "../lib/getOperatorName";
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
  const [claimDialogOpen, setClaimDialogOpen] = useState(false);
  /** ``true`` when the dialog is opened as a takeover (the rack is held by someone else). */
  const [claimDialogIsTakeover, setClaimDialogIsTakeover] = useState(false);
  const [claimNote, setClaimNote] = useState("");
  const [claimBusy, setClaimBusy] = useState(false);
  const [claimError, setClaimError] = useState<string | null>(null);

  const { getLock, me, refresh: refreshLocks } = useRackLocksContext();
  const currentLock = getLock(rack);
  const heldByMe = !!currentLock && currentLock.kind === "claim" && currentLock.operator === me && !!me;
  const heldByOther =
    !!currentLock && !(currentLock.kind === "claim" && currentLock.operator === me && !!me);
  /** Only manual claims by another operator may be taken over. Active jobs cannot be
   *  "stolen" — the operation has to finish (or be cancelled at the source). */
  const canTakeOver = !!currentLock && currentLock.kind === "claim" && !heldByMe;

  const claimRack = useCallback(
    async (note: string, opts: { force?: boolean } = {}) => {
      const op = getOperatorName();
      if (!op) {
        setClaimError("Set an operator display name in Settings → General before claiming.");
        return false;
      }
      setClaimBusy(true);
      setClaimError(null);
      try {
        const res = await apiFetch(`/api/rack-locks/${encodeURIComponent(rack)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note, force: !!opts.force }),
        });
        const data = (await res.json()) as { ok?: boolean; error?: string };
        if (!res.ok || data.ok === false) {
          setClaimError(data.error || `HTTP ${res.status}`);
          return false;
        }
        await refreshLocks();
        return true;
      } catch (err) {
        setClaimError(err instanceof Error ? err.message : "Claim failed");
        return false;
      } finally {
        setClaimBusy(false);
      }
    },
    [rack, refreshLocks],
  );

  const openClaimDialog = useCallback((takeover: boolean) => {
    setClaimNote("");
    setClaimError(null);
    setClaimDialogIsTakeover(takeover);
    setClaimDialogOpen(true);
  }, []);

  const releaseRack = useCallback(async () => {
    setClaimBusy(true);
    setClaimError(null);
    try {
      const res = await apiFetch(`/api/rack-locks/${encodeURIComponent(rack)}`, {
        method: "DELETE",
      });
      const data = (await res.json()) as { ok?: boolean; error?: string };
      if (!res.ok || data.ok === false) {
        setClaimError(data.error || `HTTP ${res.status}`);
        return;
      }
      await refreshLocks();
    } catch (err) {
      setClaimError(err instanceof Error ? err.message : "Release failed");
    } finally {
      setClaimBusy(false);
    }
  }, [rack, refreshLocks]);

  // Auto-claim on mount when the user opted in, release on unmount.
  useEffect(() => {
    if (!isAutoClaimEnabled() || !getOperatorName()) return;
    let cancelled = false;
    (async () => {
      const ok = await claimRack("auto-claimed on rack detail open");
      if (!ok && !cancelled) {
        // Silently swallow — banner above already shows the lock state.
      }
    })();
    return () => {
      cancelled = true;
      if (isAutoClaimEnabled() && getOperatorName()) {
        void releaseRack();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rack]);

  const nodes = useMemo(() => {
    if (!row) return [];
    return parseNodesFromRack(rack, row.ct_down_unk, physicalRf0);
  }, [rack, row, physicalRf0]);

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
          <Box sx={{ flexGrow: 1 }} />
          {heldByMe && (
            <Flex direction="row" gap="3" align="center">
              <Tag color="green" kind="outline" density="compact" readOnly>
                You hold this rack
              </Tag>
              <Button kind="tertiary" size="small" disabled={claimBusy} onClick={() => void releaseRack()}>
                Release
              </Button>
            </Flex>
          )}
          {!heldByMe && !heldByOther && (
            <Button kind="secondary" size="small" onClick={() => openClaimDialog(false)}>
              <Lock size={14} style={{ marginRight: 4 }} />
              Claim rack
            </Button>
          )}
        </Box>

        {heldByOther && currentLock && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="warning" kind="inline">
              <Flex direction="row" gap="3" align="center" wrap="wrap">
                <Text kind="body/regular/md" style={{ flexGrow: 1 }}>
                  <strong>{currentLock.operator || "Another operator"}</strong>
                  {" is currently "}
                  {currentLock.kind === "claim" ? "claiming" : "running"}
                  {currentLock.detail ? ` ${currentLock.detail}` : ""}
                  {" on this rack. Operations remain available — coordinate before proceeding."}
                </Text>
                {canTakeOver && (
                  <Button
                    kind="secondary"
                    size="small"
                    disabled={claimBusy}
                    onClick={() => openClaimDialog(true)}
                  >
                    <Lock size={14} style={{ marginRight: 4 }} />
                    Take over
                  </Button>
                )}
              </Flex>
            </Banner>
          </div>
        )}
        {claimError && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="error" kind="inline">
              <Text kind="body/regular/sm">{claimError}</Text>
            </Banner>
          </div>
        )}

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
              logDir={logDir}
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

      <Dialog open={claimDialogOpen} onClose={() => setClaimDialogOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, fontWeight: 600 }}>
          <Lock size={18} aria-hidden="true" />
          {claimDialogIsTakeover ? `Take over ${rack}` : `Claim ${rack}`}
        </DialogTitle>
        <DialogContent dividers>
          <Flex direction="col" gap="3">
            {claimDialogIsTakeover && currentLock ? (
              <Banner status="warning" kind="inline">
                <Text kind="body/regular/sm">
                  This will replace <strong>{currentLock.operator || "another operator"}</strong>'s
                  manual claim. They will lose ownership immediately — coordinate before taking over.
                </Text>
              </Banner>
            ) : (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                Claims are advisory — they tell other operators you intend to work on this rack
                for the next 30 minutes (refreshable, capped at 6 h).
              </Text>
            )}
            <TextField
              autoFocus
              fullWidth
              size="small"
              label="Note (optional)"
              placeholder={
                claimDialogIsTakeover
                  ? "e.g. previous operator left for the day"
                  : "e.g. swapping switch on ct03"
              }
              value={claimNote}
              onChange={(e) => setClaimNote(e.target.value)}
            />
            {claimError && (
              <Banner status="error" kind="inline">
                <Text kind="body/regular/sm">{claimError}</Text>
              </Banner>
            )}
          </Flex>
        </DialogContent>
        <DialogActions sx={{ p: 2 }}>
          <Button kind="tertiary" size="small" onClick={() => setClaimDialogOpen(false)} disabled={claimBusy}>
            Cancel
          </Button>
          <Button
            kind="primary"
            color={claimDialogIsTakeover ? "danger" : undefined}
            size="small"
            disabled={claimBusy}
            onClick={async () => {
              const ok = await claimRack(claimNote, { force: claimDialogIsTakeover });
              if (ok) setClaimDialogOpen(false);
            }}
          >
            {claimBusy
              ? claimDialogIsTakeover
                ? "Taking over..."
                : "Claiming..."
              : claimDialogIsTakeover
                ? "Take over"
                : "Claim"}
          </Button>
        </DialogActions>
      </Dialog>
    </RackCommandProvider>
  );
}
