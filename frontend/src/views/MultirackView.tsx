import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import Box from "@mui/material/Box";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import { Card, Text, Tag, Button, Banner, Flex } from "@nvidia/foundations-react-core";
import { Layers, LineChart } from "lucide-react";
import Tooltip from "@mui/material/Tooltip";
import IconButton from "@mui/material/IconButton";
import { PageHeader } from "../components/layout/PageHeader";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import { TailLogDialog } from "../components/TailLogDialog";
import {
  buildMultirackTailBody,
  openRackLogInNewTab,
  validationOngoing,
  type TailLogStreamBody,
} from "../lib/rackLog";
import type { RackRow, RackStatusResponse } from "../types/api";

interface MultirackViewProps {
  data: RackStatusResponse | null;
  logDir: string;
}

function isRackEligible(row: RackRow): { eligible: boolean; reason: string } {
  if (row.nemo !== "done") return { eligible: false, reason: "need validation done" };
  const sinfoStates = row.sinfo.split(",").map((s) => s.trim().split("=")[0].toLowerCase()).filter(Boolean);
  if (sinfoStates.length === 0) return { eligible: false, reason: "no sinfo data" };
  if (sinfoStates.some((s) => s !== "idle")) return { eligible: false, reason: `sinfo ${row.sinfo}` };
  if (row.ct_down_unk.includes("DN:")) return { eligible: false, reason: "has DN nodes" };
  return { eligible: true, reason: "" };
}

type TagColor = "green" | "red" | "yellow" | "blue" | "gray";

function nemoColor(nemo: string): TagColor {
  if (nemo === "done") return "green";
  if (nemo === "fail") return "red";
  if (nemo.startsWith("ongoing")) return "yellow";
  if (nemo === "pending") return "blue";
  return "gray";
}

interface MultirackStatusRow {
  log_file: string;
  display_rack: string;
  nemo: string;
  last_job: string;
  sinfo: string;
}

/** log_file from API is usually a basename; strip any path for parser ?log_file= */
function logBasenameForParser(logFile: string): string {
  const s = logFile.replace(/\\/g, "/").trim();
  if (!s) return "";
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

export function MultirackView({ data, logDir }: MultirackViewProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [launching, setLaunching] = useState(false);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [jobPhase, setJobPhase] = useState<string>("");
  const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" | "info" }>({
    open: false, message: "", severity: "info",
  });

  // Multirack status
  const [mrRows, setMrRows] = useState<MultirackStatusRow[]>([]);
  const [mrLoading, setMrLoading] = useState(false);
  const [mrError, setMrError] = useState("");

  const [parserOpen, setParserOpen] = useState(false);
  const [parserRack, setParserRack] = useState("");
  const [parserLogBasename, setParserLogBasename] = useState<string | undefined>();
  const [parserJobId, setParserJobId] = useState<string | undefined>();
  const [parserInitialTab, setParserInitialTab] = useState(1);

  const [tailStream, setTailStream] = useState<{ title: string; body: TailLogStreamBody } | null>(null);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const openMultirackParser = useCallback((row: MultirackStatusRow, initialTab: number) => {
    const bn = logBasenameForParser(row.log_file);
    if (!bn) return;
    setParserRack(row.display_rack || "");
    setParserLogBasename(bn);
    setParserJobId(row.last_job && row.last_job !== "-" ? row.last_job : undefined);
    setParserInitialTab(initialTab);
    setParserOpen(true);
  }, []);

  const openMultirackLog = useCallback(
    async (row: MultirackStatusRow) => {
      const bn = logBasenameForParser(row.log_file);
      if (!bn) {
        setSnackbar({ open: true, message: "No log file for this row.", severity: "error" });
        return;
      }
      const r = await openRackLogInNewTab(logDir, { log_file: bn });
      if (!r.ok) {
        setSnackbar({ open: true, message: r.error, severity: "error" });
      }
    },
    [logDir]
  );

  const startMultirackTail = useCallback(
    (row: MultirackStatusRow) => {
      const bn = logBasenameForParser(row.log_file);
      if (!bn) {
        setSnackbar({ open: true, message: "No log file for this row.", severity: "error" });
        return;
      }
      setTailStream({
        title: `Tail log — ${row.display_rack || bn}`,
        body: buildMultirackTailBody(logDir, bn),
      });
    },
    [logDir]
  );

  const rows = data?.rows || [];

  const eligibilityMap = useMemo(() => {
    const map = new Map<string, { eligible: boolean; reason: string }>();
    for (const row of rows) {
      map.set(row.rack, isRackEligible(row));
    }
    return map;
  }, [rows]);

  const eligibleCount = useMemo(() => {
    let count = 0;
    for (const v of eligibilityMap.values()) {
      if (v.eligible) count++;
    }
    return count;
  }, [eligibilityMap]);

  const handleToggle = (rack: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(rack)) next.delete(rack);
      else next.add(rack);
      return next;
    });
  };

  const handleSelectAllEligible = () => {
    const allEligible = rows.filter((r) => eligibilityMap.get(r.rack)?.eligible).map((r) => r.rack);
    if (selected.size === allEligible.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(allEligible));
    }
  };

  const handleOpenDialog = () => {
    setSelected(new Set());
    setDialogOpen(true);
  };

  const handleLaunch = async () => {
    if (selected.size === 0) return;
    setLaunching(true);
    try {
      const res = await fetch("/api/run-nemo-xrack-async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ racks: [...selected].sort(), log_dir: logDir }),
      });
      const json = await res.json();
      if (json.ok) {
        setActiveJobId(json.job_id);
        setJobPhase("starting");
        setDialogOpen(false);
        setSnackbar({ open: true, message: `Multirack validation launched (${selected.size} racks)`, severity: "success" });
      } else {
        setSnackbar({ open: true, message: json.error || "Launch failed", severity: "error" });
      }
    } catch (err) {
      setSnackbar({ open: true, message: `Network error: ${err instanceof Error ? err.message : "unknown"}`, severity: "error" });
    } finally {
      setLaunching(false);
    }
  };

  // Poll job status
  useEffect(() => {
    if (!activeJobId) return;
    const poll = async () => {
      try {
        const res = await fetch(`/api/run-nemo-xrack-status?job_id=${encodeURIComponent(activeJobId)}`);
        const json = await res.json();
        const phase = json.phase || "unknown";
        setJobPhase(phase);
        if (phase === "done" || phase === "error" || phase === "timeout") {
          setSnackbar({
            open: true,
            message: phase === "done" ? "Multirack validation completed" : `Multirack validation ${phase}`,
            severity: phase === "done" ? "success" : "error",
          });
          setActiveJobId(null);
        }
      } catch {
        // keep polling
      }
    };
    poll();
    pollRef.current = setInterval(poll, 5000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [activeJobId]);

  // Fetch multirack status
  const fetchMultirackStatus = useCallback(async () => {
    setMrLoading(true);
    setMrError("");
    try {
      const res = await fetch(`/api/multirack-status?log_dir=${encodeURIComponent(logDir)}`);
      const json = await res.json();
      if (json.ok && Array.isArray(json.rows)) {
        setMrRows(json.rows.map((r: Record<string, string>) => ({
          log_file: r.log_file || r.basename || "",
          display_rack: r.rack || r.display_rack || "",
          nemo: r.nemo || "-",
          last_job: r.last_job || "-",
          sinfo: r.sinfo || "-",
        })));
      } else {
        setMrRows([]);
        if (json.error) setMrError(json.error);
      }
    } catch (err) {
      setMrError(err instanceof Error ? err.message : "Failed to fetch");
      setMrRows([]);
    } finally {
      setMrLoading(false);
    }
  }, [logDir]);

  useEffect(() => {
    fetchMultirackStatus();
  }, [fetchMultirackStatus]);

  return (
    <Box sx={{ maxWidth: 1400, mx: "auto", p: 4 }}>
      <PageHeader
        title="Multirack Validation"
        description="Cross-rack NEMO validation runs spanning multiple racks."
        actions={
          <Button kind="primary" size="small" onClick={handleOpenDialog} disabled={!data || rows.length === 0}>
            <Layers size={16} style={{ marginRight: 6 }} />
            Launch Multirack Validation
          </Button>
        }
      />

      {/* Active job banner */}
      {activeJobId && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="info">
            <Flex gap="2" align="center">
              <CircularProgress size={16} />
              <span>Multirack validation running — phase: <strong>{jobPhase}</strong></span>
            </Flex>
          </Banner>
        </div>
      )}

      {/* Multirack status table */}
      {mrLoading && (
        <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
          <CircularProgress />
        </Box>
      )}

      {mrError && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="warning">
            {mrError}
          </Banner>
        </div>
      )}

      {!mrLoading && mrRows.length > 0 && (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Racks</TableCell>
                <TableCell>Validation</TableCell>
                <TableCell>Last Job</TableCell>
                <TableCell>Sinfo</TableCell>
                <TableCell>Log File</TableCell>
                <TableCell align="right">Results</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {mrRows.map((row, i) => {
                const hasLog = Boolean(logBasenameForParser(row.log_file));
                const live = validationOngoing(row.nemo);
                return (
                  <TableRow key={i} hover>
                    <TableCell sx={{ fontWeight: 600 }}>{row.display_rack}</TableCell>
                    <TableCell>
                      <Tag color={nemoColor(row.nemo)} kind="solid" readOnly density="compact">{row.nemo}</Tag>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>{row.last_job}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{row.sinfo}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {row.log_file.length > 50 ? "..." + row.log_file.slice(-47) : row.log_file}
                      </Text>
                    </TableCell>
                    <TableCell align="right">
                      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 0.5, flexWrap: "wrap" }}>
                        {live ? (
                          <MuiButton
                            size="small"
                            variant="outlined"
                            disabled={!hasLog}
                            onClick={() => startMultirackTail(row)}
                          >
                            Tail Log
                          </MuiButton>
                        ) : (
                          <MuiButton
                            size="small"
                            variant="outlined"
                            disabled={!hasLog}
                            onClick={() => void openMultirackLog(row)}
                          >
                            Open Log
                          </MuiButton>
                        )}
                        <Tooltip title={hasLog ? "NCCL / NEMO charts and Grafana (parser.sh --json)" : "No log file"}>
                          <span>
                            <IconButton
                              size="small"
                              disabled={!hasLog}
                              onClick={() => openMultirackParser(row, 1)}
                              aria-label="Open parser results"
                            >
                              <LineChart size={18} />
                            </IconButton>
                          </span>
                        </Tooltip>
                      </Box>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {!mrLoading && mrRows.length === 0 && !mrError && (
        <Card style={{ padding: 48, textAlign: "center" }}>
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            No multirack validation logs found. Launch a multirack validation to see results here.
          </Text>
        </Card>
      )}

      {/* Launch dialog */}
      <Dialog open={dialogOpen} onClose={() => !launching && setDialogOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ fontWeight: 600 }}>Launch Multirack Validation</DialogTitle>
        <DialogContent dividers>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
            Select racks with validation <strong>done</strong>, Slurm sinfo showing <strong>only idle</strong>,
            and no DN trays in device status. Others are shown but not selectable.
          </Text>

          <Box sx={{ mb: 2 }}>
            <MuiButton size="small" onClick={handleSelectAllEligible}>
              {selected.size === eligibleCount ? "Clear all" : `Select all eligible (${eligibleCount})`}
            </MuiButton>
          </Box>

          <TableContainer sx={{ maxHeight: "50vh" }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell padding="checkbox" />
                  <TableCell>Rack</TableCell>
                  <TableCell>Validation</TableCell>
                  <TableCell>Sinfo</TableCell>
                  <TableCell>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((row) => {
                  const { eligible, reason } = eligibilityMap.get(row.rack) || { eligible: false, reason: "unknown" };
                  return (
                    <TableRow
                      key={row.rack}
                      hover={eligible}
                      onClick={() => eligible && handleToggle(row.rack)}
                      sx={{
                        cursor: eligible ? "pointer" : "default",
                        opacity: eligible ? 1 : 0.5,
                      }}
                    >
                      <TableCell padding="checkbox">
                        <Checkbox
                          checked={selected.has(row.rack)}
                          disabled={!eligible}
                          onChange={() => handleToggle(row.rack)}
                        />
                      </TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>{row.rack}</TableCell>
                      <TableCell>
                        <Tag color={nemoColor(row.nemo)} kind="solid" readOnly density="compact">{row.nemo}</Tag>
                      </TableCell>
                      <TableCell>
                        <Text kind="body/regular/sm">{row.sinfo}</Text>
                      </TableCell>
                      <TableCell>
                        {eligible ? (
                          <Tag color="green" kind="outline" readOnly density="compact">eligible</Tag>
                        ) : (
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                            {reason}
                          </Text>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>

          {selected.size > 0 && (
            <Text kind="body/semibold/sm" style={{ marginTop: 12, display: "block" }}>
              {selected.size} rack(s) selected
            </Text>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <MuiButton onClick={() => setDialogOpen(false)} disabled={launching}>
            Cancel
          </MuiButton>
          <MuiButton
            onClick={handleLaunch}
            variant="contained"
            disabled={launching || selected.size === 0}
            startIcon={launching ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            {launching ? "Launching..." : `Submit (${selected.size} racks)`}
          </MuiButton>
        </DialogActions>
      </Dialog>

      {/* Snackbar */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={5000}
        onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
          severity={snackbar.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>

      <TailLogDialog
        open={tailStream !== null}
        onClose={() => setTailStream(null)}
        title={tailStream?.title ?? "Tail log"}
        payload={tailStream?.body ?? null}
      />

      <ParserResultsDialog
        open={parserOpen}
        onClose={() => {
          setParserOpen(false);
          setParserLogBasename(undefined);
          setParserJobId(undefined);
          setParserInitialTab(1);
        }}
        rack={parserRack}
        jobId={parserJobId}
        logDir={logDir}
        logFileBasename={parserLogBasename}
        initialTabIndex={parserInitialTab}
      />
    </Box>
  );
}
