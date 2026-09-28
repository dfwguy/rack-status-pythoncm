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
import { useState, useMemo, useCallback, useEffect, lazy, Suspense } from "react";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Checkbox from "@mui/material/Checkbox";
import Paper from "@mui/material/Paper";
import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import InputAdornment from "@mui/material/InputAdornment";
import Link from "@mui/material/Link";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import { Tag, Text, Flex, Button } from "@nvidia/foundations-react-core";
import { MultiSelectCombobox } from "./MultiSelectCombobox";
import Tooltip from "@mui/material/Tooltip";
import {
  CircleCheck,
  Cpu,
  Shuffle,
  XCircle,
  Monitor,
  Search,
  Ellipsis,
  Server,
  ArrowDownToLine,
  ArrowUpFromLine,
  FileText,
  ScrollText,
  Power,
  MessageSquarePlus,
  Pencil,
  Trash2,
  LineChart,
  ChevronDown,
} from "lucide-react";
import { useRackCommandsStore } from "../context/RackCommandsStoreContext";
import { RackTableCommandIndicator } from "./RackTableCommandIndicator";
// chart.js (~493 kB of source) rides in with the parser charts, and this table is on the
// dashboard -- the first screen every operator sees. Lazy *and* gated on `parserOpen`
// below; see the note in App.tsx for why one without the other buys nothing.
const ParserResultsDialog = lazy(() =>
  import("./ParserResultsDialog").then((m) => ({ default: m.ParserResultsDialog })),
);
import { GrafanaLinksMenu } from "./GrafanaLinksMenu";
import { TailLogDialog } from "./TailLogDialog";
import { HwCheckRunDialog } from "./rack/HwCheckRunDialog";
import {
  buildSingleRackTailBody,
  openRackLogInNewTab,
  validationOngoing,
  type TailLogStreamBody,
} from "../lib/rackLog";
import { validationStatusTooltip } from "../lib/validationStatusTooltip";
import {
  VALIDATION_CATEGORIES,
  VALIDATION_STAGES,
  nemoCategory,
  stageStates,
  stageStateColor,
  stageStateLabel,
} from "../lib/validationStages";
import type { RackRow } from "../types/api";
import type { NvisNamingSchema } from "../types/naming";
import {
  attachLogDir,
  fullRackCt01to18NodeSet,
  llamaNodesBody,
  slurmTargetBody,
  specsCommandBody,
  hwCheckOptionsBody,
  validationFamilyBody,
} from "../lib/rackCommandBodies";
import { apiFetch, openApiUrl } from "../lib/apiFetch";

const ICON_SIZE = 14;

interface RackTableProps {
  rows: RackRow[];
  comments: Record<string, string>;
  logDir?: string;
  naming?: NvisNamingSchema;
  onRackSelect?: (rack: string) => void;
  onCommentChange?: (rack: string, comment: string | null) => void;
  /** When set with ``onValidationFiltersChange``, validation filter state is controlled by the parent (e.g. summary chips). */
  validationFilters?: string[];
  onValidationFiltersChange?: (filters: string[]) => void;
}

type SortCol = "rack" | "nemo" | "sinfo";
type SortDir = "asc" | "desc";
type ActionKind = "drain" | "undrain" | "cancel" | "power_up";
type MenuKind = "sinfo" | "job" | "device" | "comment";

const VALIDATION_OPTIONS: string[] = VALIDATION_CATEGORIES;
const SINFO_OPTIONS = ["idle", "alloc", "mix", "drain", "down", "comp"];

const ACTION_LABELS: Record<ActionKind, { title: string; description: (rack: string, jobId?: string) => string; confirm: string }> = {
  drain: {
    title: "Drain Nodes",
    description: (rack) => `This will drain all nodes in ${rack}, preventing new Slurm jobs from scheduling on them.`,
    confirm: "Drain",
  },
  undrain: {
    title: "Undrain Nodes",
    description: (rack) => `This will undrain (resume) all nodes in ${rack}, re-enabling Slurm job scheduling.`,
    confirm: "Undrain",
  },
  cancel: {
    title: "Cancel Job",
    description: (rack, jobId) => `This will cancel Slurm job ${jobId ?? "?"} running on ${rack}.`,
    confirm: "Cancel Job",
  },
  power_up: {
    title: "Power Up Nodes",
    description: (rack) => `This will power-cycle the down nodes in ${rack} (power off then on).`,
    confirm: "Power Up",
  },
};

function nemoSortKey(nemo: string): number {
  if (nemo === "done") return 0;
  if (nemo.startsWith("ongoing")) return 1;
  if (nemo === "pending") return 2;
  return 3;
}

function extractSinfoStates(sinfo: string): string[] {
  if (sinfo === "-" || sinfo === "?") return [];
  return sinfo.split(",").map((s) => {
    const [stateRaw] = s.trim().split("=");
    return stateRaw.trim().toLowerCase();
  }).filter(Boolean);
}

function hasDownDevices(ct: string): boolean {
  return ct.includes("DN:") || ct.includes("INST:");
}

type TagColor = "green" | "blue" | "teal" | "yellow" | "red" | "gray";

function validationTagColor(state: string): TagColor {
  switch (state) {
    case "done": return "green";
    case "fail": return "red";
    case "ongoing": return "yellow";
    case "pending": return "blue";
    default: return "gray";
  }
}

function sinfoTagColor(state: string): TagColor {
  switch (state) {
    case "idle": return "green";
    case "alloc": return "blue";
    case "mix": return "teal";
    case "drain": return "yellow";
    case "down": return "red";
    case "comp": return "blue";
    default: return "gray";
  }
}

function sinfoIcon(state: string) {
  switch (state) {
    case "idle": return <CircleCheck size={ICON_SIZE} />;
    case "alloc": return <Cpu size={ICON_SIZE} />;
    case "mix": return <Shuffle size={ICON_SIZE} />;
    case "drain": return <CircleCheck size={ICON_SIZE} />;
    case "down": return <XCircle size={ICON_SIZE} />;
    case "comp": return <Monitor size={ICON_SIZE} />;
    default: return null;
  }
}

/** Compact colored badges for each per-rack validation stage (NCCL, NeMo). */
function StageBadges({ row }: { row: RackRow }) {
  const states = stageStates(row);
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
      {VALIDATION_STAGES.map((stage) => {
        const state = states[stage.key];
        return (
          <Tooltip
            key={stage.key}
            title={`${stage.label} — ${stage.tool}: ${stageStateLabel(state)}`}
            enterDelay={350}
            arrow
          >
            <span style={{ display: "inline-flex", cursor: "default" }}>
              <Tag color={stageStateColor(state)} kind="outline" readOnly density="compact">
                {stage.short} {stage.tool}
              </Tag>
            </span>
          </Tooltip>
        );
      })}
    </Box>
  );
}

function ValidationTag({ row }: { row: RackRow }) {
  const nemo = row.nemo;
  const color = validationTagColor(nemoCategory(nemo));
  const tip = validationStatusTooltip(row);
  const overall = (
    <Tag color={color} kind="solid" readOnly density="compact">
      {nemo}
    </Tag>
  );
  const overallWithTip = !tip ? (
    overall
  ) : (
    <Tooltip
      title={<span style={{ display: "block", maxWidth: 420, whiteSpace: "pre-wrap" }}>{tip}</span>}
      enterDelay={350}
      arrow
    >
      <span style={{ display: "inline-flex", cursor: "default" }}>{overall}</span>
    </Tooltip>
  );
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0.5, alignItems: "flex-start" }}>
      {overallWithTip}
      <StageBadges row={row} />
    </Box>
  );
}

function SinfoTags({ sinfo }: { sinfo: string }) {
  if (sinfo === "-" || sinfo === "?") {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{sinfo}</Text>;
  }
  const parts = sinfo.split(",").map((s) => s.trim()).filter(Boolean);
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
      {parts.map((part, i) => {
        const [stateRaw, count] = part.includes("=") ? part.split("=") : [part, ""];
        const state = stateRaw.trim().toLowerCase();
        const label = count ? `${state}=${count}` : state;
        const icon = sinfoIcon(state);
        return (
          <Tag key={i} color={sinfoTagColor(state)} kind="solid" readOnly density="compact">
            {icon && <span style={{ display: "inline-flex", marginRight: 4, verticalAlign: "middle" }}>{icon}</span>}
            {label}
          </Tag>
        );
      })}
    </Box>
  );
}

/** Tray bodies after UP:/DN:/INST:/UNK: prefixes (legacy `ct_down_unk` segments). */
function parseNodesStatusSegments(ct: string): {
  up: string[];
  dn: string[];
  inst: string[];
  unk: string[];
  other: string[];
} {
  if (ct === "-" || ct === "?") return { up: [], dn: [], inst: [], unk: [], other: [] };
  const up: string[] = [];
  const dn: string[] = [];
  const inst: string[] = [];
  const unk: string[] = [];
  const other: string[] = [];
  for (const seg of ct.split(";").map((s) => s.trim()).filter(Boolean)) {
    if (/^up:/i.test(seg)) up.push(seg.replace(/^up:/i, "").trim());
    else if (/^dn:/i.test(seg)) dn.push(seg.replace(/^dn:/i, "").trim());
    else if (/^inst:/i.test(seg)) inst.push(seg.replace(/^inst:/i, "").trim());
    else if (/^unk:/i.test(seg)) unk.push(seg.replace(/^unk:/i, "").trim());
    else other.push(seg);
  }
  return { up, dn, inst, unk, other };
}

function StatusLine({
  prefix,
  bodies,
  tagColor,
}: {
  prefix: string;
  bodies: string[];
  tagColor: TagColor;
}) {
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5, alignItems: "center", minWidth: 0 }}>
      <Box
        component="span"
        sx={{
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          fontSize: 12,
          fontWeight: 600,
          color: "var(--text-color-secondary)",
          flexShrink: 0,
        }}
      >
        {prefix}
      </Box>
      {bodies.length === 0 ? (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>—</Text>
      ) : (
        bodies.map((body, i) => (
          <Tag key={`${prefix}-${i}`} color={tagColor} kind="solid" readOnly density="compact">
            {body}
          </Tag>
        ))
      )}
    </Box>
  );
}

function nodeStatusHasTypedSegments(ct: string): boolean {
  const s = ct.trim();
  return /^(dn|inst|up|unk):/i.test(s) || /;\s*(dn|inst|up|unk):/i.test(s);
}

function NodeStatusTags({
  ct,
  rowHasDown,
  onOpenDeviceMenu,
  menuDisabled,
}: {
  ct: string;
  rowHasDown: boolean;
  onOpenDeviceMenu: (e: React.MouseEvent<HTMLElement>) => void;
  menuDisabled?: boolean;
}) {
  if (ct === "-" || ct === "?") {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{ct}</Text>;
  }
  if (!nodeStatusHasTypedSegments(ct)) {
    return (
      <Box sx={{ display: "flex", gap: 0.5, alignItems: "flex-start", width: "100%", minWidth: 0 }}>
        <Text kind="body/regular/sm" style={{ fontFamily: "monospace", color: "var(--text-color-secondary)", flex: 1, minWidth: 0, wordBreak: "break-word" }}>
          {ct}
        </Text>
        {rowHasDown && (
          <IconButton size="small" disabled={menuDisabled} onClick={onOpenDeviceMenu} sx={{ flexShrink: 0 }}>
            <Ellipsis size={16} />
          </IconButton>
        )}
      </Box>
    );
  }
  const { up, dn, inst, unk, other } = parseNodesStatusSegments(ct);
  return (
    <Box sx={{ display: "flex", gap: 0.5, alignItems: "center", width: "100%", minWidth: 0 }}>
      <Box
        sx={{
          flex: 1,
          minWidth: 0,
          display: "flex",
          flexDirection: "row",
          flexWrap: "wrap",
          gap: 0.5,
          alignItems: "center",
        }}
      >
        <StatusLine prefix="UP:" bodies={up} tagColor="green" />
        <StatusLine prefix="DN:" bodies={dn} tagColor="red" />
        <StatusLine prefix="INST:" bodies={inst} tagColor="blue" />
        {unk.length > 0 && <StatusLine prefix="UNK:" bodies={unk} tagColor="yellow" />}
        {other.length > 0 &&
          other.map((o, i) => (
            <Tag key={`o-${i}`} color="gray" kind="solid" readOnly density="compact">
              {o}
            </Tag>
          ))}
      </Box>
      {rowHasDown && (
        <IconButton size="small" disabled={menuDisabled} onClick={onOpenDeviceMenu} sx={{ flexShrink: 0 }}>
          <Ellipsis size={16} />
        </IconButton>
      )}
    </Box>
  );
}

export function RackTable({
  rows,
  comments,
  logDir,
  naming,
  onRackSelect,
  onCommentChange,
  validationFilters: validationFiltersProp,
  onValidationFiltersChange,
}: RackTableProps) {
  const [sortCol, setSortCol] = useState<SortCol>("rack");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);

  const [search, setSearch] = useState("");
  const [internalValidationFilters, setInternalValidationFilters] = useState<string[]>([]);
  const validationFilters = validationFiltersProp ?? internalValidationFilters;
  const setValidationFilters = useCallback(
    (v: string[]) => {
      if (onValidationFiltersChange) {
        onValidationFiltersChange(v);
      } else {
        setInternalValidationFilters(v);
      }
    },
    [onValidationFiltersChange]
  );

  const validationFilterKey = validationFilters.slice().sort().join(",");
  useEffect(() => {
    setPage(0);
  }, [validationFilterKey]);

  const [sinfoFilters, setSinfoFilters] = useState<string[]>([]);
  const [deviceFilters, setDeviceFilters] = useState<string[]>([]);

  // Parser results dialog (initial tab: 0 Summary, 1 NCCL — legacy opens NCCL first for rack review)
  const [parserOpen, setParserOpen] = useState(false);
  const [parserInitialTab, setParserInitialTab] = useState(0);
  const [parserRack, setParserRack] = useState("");
  const [parserJobId, setParserJobId] = useState<string | undefined>();

  const openParserForRow = useCallback((row: RackRow, initialTab: number) => {
    setParserRack(row.rack);
    setParserJobId(row.last_job !== "-" ? row.last_job : undefined);
    setParserInitialTab(initialTab);
    setParserOpen(true);
  }, []);

  const [tailStream, setTailStream] = useState<{ title: string; body: TailLogStreamBody } | null>(null);

  // Declared above its first consumer (openLogForRow, just below). When it sat further down,
  // the React Compiler refused to compile this component at all -- which silently disabled
  // the memoisation on every useMemo/useCallback block in this file.
  const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" }>({
    open: false, message: "", severity: "success",
  });

  const openLogForRow = useCallback(
    async (row: RackRow) => {
      const job = row.last_job.trim() && row.last_job !== "-" ? row.last_job : undefined;
      const r = await openRackLogInNewTab(logDir, { rack: row.rack, job_id: job });
      if (!r.ok) {
        setSnackbar({ open: true, message: r.error, severity: "error" });
      }
    },
    [logDir]
  );

  const startTailForRow = useCallback(
    (row: RackRow) => {
      const job = row.last_job.trim() && row.last_job !== "-" ? row.last_job : undefined;
      setTailStream({
        title: `Tail log — ${row.rack}`,
        body: buildSingleRackTailBody(logDir, row.rack, job),
      });
    },
    [logDir]
  );

  // Comment edit dialog
  const [commentDialogOpen, setCommentDialogOpen] = useState(false);
  const [commentDialogRack, setCommentDialogRack] = useState("");
  const [commentDialogText, setCommentDialogText] = useState("");
  const [commentSaving, setCommentSaving] = useState(false);

  // Menus — one anchor per menu type
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [menuKind, setMenuKind] = useState<MenuKind | null>(null);
  const [menuRow, setMenuRow] = useState<RackRow | null>(null);

  // Confirmation + feedback
  const [confirmAction, setConfirmAction] = useState<{ kind: ActionKind; rack: string; jobId?: string } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const store = useRackCommandsStore();
  const [selectMode, setSelectMode] = useState(false);
  const [selectedRacks, setSelectedRacks] = useState<Set<string>>(() => new Set());
  const [runMenuAnchor, setRunMenuAnchor] = useState<HTMLElement | null>(null);
  const [hwBulkDialogOpen, setHwBulkDialogOpen] = useState(false);

  const nodesFullRack = useCallback((rack: string) => fullRackCt01to18NodeSet(rack), []);

  const toggleSelectMode = useCallback(() => {
    setSelectMode((m) => {
      if (m) {
        setSelectedRacks(new Set());
        setRunMenuAnchor(null);
      }
      return !m;
    });
  }, []);

  const toggleRackSelected = useCallback((rack: string) => {
    setSelectedRacks((prev) => {
      const n = new Set(prev);
      if (n.has(rack)) n.delete(rack);
      else n.add(rack);
      return n;
    });
  }, []);

  const selectedRackList = useMemo(
    () => [...selectedRacks].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })),
    [selectedRacks],
  );

  const fireBulkParallel = useCallback(
    (label: string, perRack: (rack: string) => void) => {
      setRunMenuAnchor(null);
      for (const rack of selectedRackList) {
        perRack(rack);
      }
      setSnackbar({
        open: true,
        message: `${label}: started on ${selectedRackList.length} rack(s) (ct[01–18] each, parallel)`,
        severity: "success",
      });
    },
    [selectedRackList],
  );

  const runHwCheckBulk = useCallback(
    (opts: { preset?: string; checker_args?: string }) => {
      setHwBulkDialogOpen(false);
      const ld = logDir?.trim() ?? "";
      if (!selectedRackList.length) {
        setSnackbar({ open: true, message: "Choose at least one rack", severity: "error" });
        return;
      }
      for (const rack of selectedRackList) {
        void store.runAsyncReset(rack, "Hardware check", "hw", "/api/hw-check-async", {
          ...specsCommandBody(rack, ld, nodesFullRack(rack), naming),
          ...hwCheckOptionsBody(opts),
        });
      }
      setSnackbar({
        open: true,
        message: `Hardware check: started on ${selectedRackList.length} rack(s) (ct[01–18] each, parallel)`,
        severity: "success",
      });
    },
    [logDir, naming, nodesFullRack, selectedRackList, store],
  );

  const handleBulkRun = useCallback(
    (key: string) => {
      if (!selectedRackList.length) {
        setSnackbar({ open: true, message: "Choose at least one rack", severity: "error" });
        setRunMenuAnchor(null);
        return;
      }
      if (["nemo", "llama", "stress", "nvloom", "clusterkit", "llmb"].includes(key) && !logDir?.trim()) {
        setSnackbar({
          open: true,
          message: "Set log directory (toolbar / settings) before launching validation or Llama jobs.",
          severity: "error",
        });
        setRunMenuAnchor(null);
        return;
      }
      const ld = logDir?.trim() ?? "";
      switch (key) {
        case "nemo":
          fireBulkParallel("NEMO validation", (rack) =>
            void store.runAsyncValidationFamily(
              rack,
              "NEMO validation",
              "validation",
              "/api/run-validation-async",
              (jid) => `/api/run-validation-status?job_id=${encodeURIComponent(jid)}`,
              validationFamilyBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "llama":
          fireBulkParallel("Llama", (rack) =>
            void store.runAsyncValidationFamily(
              rack,
              "Llama",
              "validation",
              "/api/run-llama-async",
              (jid) => `/api/run-llama-status?job_id=${encodeURIComponent(jid)}`,
              llamaNodesBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "stress":
          fireBulkParallel("MNUBERGEMM stress", (rack) =>
            void store.runAsyncValidationFamily(
              rack,
              "MNUBERGEMM stress",
              "stress",
              "/api/run-mnubergemm-stress-async",
              (jid) => `/api/run-mnubergemm-stress-status?job_id=${encodeURIComponent(jid)}`,
              validationFamilyBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "nvloom":
          fireBulkParallel("nvloom", (rack) =>
            void store.runAsyncValidationFamily(
              rack,
              "nvloom",
              "validation",
              "/api/run-nvloom-async",
              (jid) => `/api/run-nvloom-status?job_id=${encodeURIComponent(jid)}`,
              validationFamilyBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "clusterkit":
          fireBulkParallel("ClusterKit", (rack) =>
            void store.runAsyncValidationFamily(
              rack,
              "ClusterKit",
              "validation",
              "/api/run-clusterkit-async",
              (jid) => `/api/run-clusterkit-status?job_id=${encodeURIComponent(jid)}`,
              validationFamilyBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "llmb":
          fireBulkParallel("NCCL Microbenchmark (LLMB)", (rack) =>
            void store.runAsyncValidationFamily(
              rack,
              "NCCL Microbenchmark (LLMB)",
              "validation",
              "/api/run-llmb-nccl-async",
              (jid) => `/api/run-llmb-nccl-status?job_id=${encodeURIComponent(jid)}`,
              validationFamilyBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "power_on":
          fireBulkParallel("Power on", (rack) =>
            void store.runAsyncReset(rack, "Power on", "power", "/api/power-on-async", specsCommandBody(rack, ld, nodesFullRack(rack), naming)),
          );
          break;
        case "gpu_reset":
          fireBulkParallel("GPU reset", (rack) =>
            void store.runAsyncReset(rack, "GPU reset", "gpu", "/api/gpu-reset-async", specsCommandBody(rack, ld, nodesFullRack(rack), naming)),
          );
          break;
        case "bug_report":
          fireBulkParallel("Bug report", (rack) =>
            void store.runAsyncReset(rack, "Bug report", "hw", "/api/bug-report-async", specsCommandBody(rack, ld, nodesFullRack(rack), naming)),
          );
          break;
        case "imex_reset":
          fireBulkParallel("IMEX reset", (rack) =>
            void store.runAsyncReset(rack, "IMEX reset", "nmxc", "/api/imex-reset-async", attachLogDir({ rack }, ld)),
          );
          break;
        case "imex_check":
          fireBulkParallel("IMEX check", (rack) =>
            void store.runAsyncReset(rack, "IMEX check", "hw", "/api/imex-check-async", attachLogDir({ rack }, ld)),
          );
          break;
        case "services_restart":
          fireBulkParallel("Restart services", (rack) =>
            void store.runAsyncReset(
              rack,
              "Restart services",
              "services",
              "/api/services-restart-async",
              specsCommandBody(rack, ld, nodesFullRack(rack), naming),
            ),
          );
          break;
        case "nvswitch_fix":
          fireBulkParallel("NVSwitch fix", (rack) =>
            void store.runAsyncReset(rack, "NVSwitch fix", "nmxc", "/api/nvswitch-fix-async", attachLogDir({ rack }, ld)),
          );
          break;
        case "nvswitch_sdn":
          fireBulkParallel("NVSwitch SDN reset", (rack) =>
            void store.runAsyncReset(rack, "NVSwitch SDN reset", "nmxc", "/api/nvswitch-sdn-reset-async", attachLogDir({ rack }, ld)),
          );
          break;
        case "nvswitch_system":
          fireBulkParallel("NVSwitch system reset", (rack) => {
            void (async () => {
              try {
                const res = await apiFetch(`/api/nvswitch-hosts?rack=${encodeURIComponent(rack)}`, { credentials: "same-origin" });
                const data = (await res.json()) as { ok?: boolean; hosts?: string[]; error?: string };
                if (!res.ok || !data.ok || !data.hosts?.length) {
                  store.openText(`NVSwitch system reset — ${rack}`, data.error || "No NVSwitch hosts", true);
                  return;
                }
                await store.runAsyncReset(rack, "NVSwitch system reset", "nmxc", "/api/nvswitch-system-reset-async", {
                  ...attachLogDir({ rack }, ld),
                  nvswitch_host: data.hosts[0],
                });
              } catch (e) {
                store.openText(`NVSwitch system reset — ${rack}`, e instanceof Error ? e.message : String(e), true);
              }
            })();
          });
          break;
        case "nvswitch_check":
          fireBulkParallel("NVSwitch check", (rack) =>
            void store.runAsyncReset(rack, "NVSwitch check", "nmxc", "/api/nvswitch-check-async", attachLogDir({ rack }, ld)),
          );
          break;
        case "nvlmapper":
          fireBulkParallel("NVLMapper", (rack) =>
            void store.runAsyncReset(rack, "NVLMapper", "nmxc", "/api/nvlmapper-async", { rack }),
          );
          break;
        case "drain":
          fireBulkParallel("Slurm drain", (rack) =>
            void store.runSyncJson(rack, "Slurm drain", "/api/scheduler-drain", slurmTargetBody(rack, nodesFullRack(rack), naming) as Record<string, unknown>),
          );
          break;
        case "undrain":
          fireBulkParallel("Slurm undrain", (rack) =>
            void store.runSyncJson(rack, "Slurm undrain", "/api/scheduler-resume", slurmTargetBody(rack, nodesFullRack(rack), naming) as Record<string, unknown>),
          );
          break;
        default:
          setRunMenuAnchor(null);
      }
    },
    [selectedRackList, logDir, naming, store, fireBulkParallel, nodesFullRack],
  );

  const openMenu = useCallback((event: React.MouseEvent<HTMLElement>, kind: MenuKind, row: RackRow) => {
    if (selectMode) return;
    setMenuAnchor(event.currentTarget);
    setMenuKind(kind);
    setMenuRow(row);
  }, [selectMode]);

  const closeMenu = useCallback(() => {
    setMenuAnchor(null);
    setMenuKind(null);
    setMenuRow(null);
  }, []);

  const requestConfirm = useCallback((kind: ActionKind) => {
    if (!menuRow) return;
    setConfirmAction({ kind, rack: menuRow.rack, jobId: menuRow.last_job });
    closeMenu();
  }, [menuRow, closeMenu]);

  const handleCheckSinfo = useCallback(() => {
    closeMenu();
    void openApiUrl("/api/scheduler-node-reasons");
  }, [closeMenu]);

  const handleOpenLog = useCallback(async () => {
    if (!menuRow) return;
    closeMenu();
    await openLogForRow(menuRow);
  }, [menuRow, closeMenu, openLogForRow]);

  const handleTailLog = useCallback(() => {
    if (!menuRow) return;
    closeMenu();
    startTailForRow(menuRow);
  }, [menuRow, closeMenu, startTailForRow]);

  const handleAddEditComment = useCallback(() => {
    if (!menuRow) return;
    setCommentDialogRack(menuRow.rack);
    setCommentDialogText(comments[menuRow.rack] || "");
    setCommentDialogOpen(true);
    closeMenu();
  }, [menuRow, comments, closeMenu]);

  const handleDeleteComment = useCallback(async () => {
    if (!menuRow) return;
    const rack = menuRow.rack;
    closeMenu();
    try {
      await apiFetch(`/api/comments/${encodeURIComponent(rack)}`, { method: "DELETE" });
      onCommentChange?.(rack, null);
      setSnackbar({ open: true, message: `Comment removed for ${rack}`, severity: "success" });
    } catch {
      setSnackbar({ open: true, message: "Failed to delete comment", severity: "error" });
    }
  }, [menuRow, closeMenu, onCommentChange]);

  const handleSaveComment = useCallback(async () => {
    setCommentSaving(true);
    try {
      const res = await apiFetch(`/api/comments/${encodeURIComponent(commentDialogRack)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment: commentDialogText }),
      });
      const data = await res.json();
      if (data.ok) {
        onCommentChange?.(commentDialogRack, commentDialogText || null);
        setSnackbar({ open: true, message: `Comment saved for ${commentDialogRack}`, severity: "success" });
      } else {
        setSnackbar({ open: true, message: data.error || "Failed to save comment", severity: "error" });
      }
    } catch {
      setSnackbar({ open: true, message: "Failed to save comment", severity: "error" });
    } finally {
      setCommentSaving(false);
      setCommentDialogOpen(false);
    }
  }, [commentDialogRack, commentDialogText, onCommentChange]);

  const executeAction = useCallback(async () => {
    if (!confirmAction) return;
    setActionLoading(true);
    const { kind, rack, jobId } = confirmAction;

    try {
      let url: string;
      let body: Record<string, string>;

      switch (kind) {
        case "drain":
          url = "/api/scheduler-drain";
          body = { rack };
          break;
        case "undrain":
          url = "/api/scheduler-resume";
          body = { rack };
          break;
        case "cancel":
          url = "/api/scheduler-cancel";
          body = { job_id: jobId ?? "", rack };
          break;
        case "power_up":
          url = "/api/power-reset";
          body = { rack };
          break;
      }

      const res = await apiFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();

      if (data.ok) {
        setSnackbar({ open: true, message: `${ACTION_LABELS[kind].title} succeeded on ${rack}`, severity: "success" });
      } else {
        setSnackbar({ open: true, message: data.error || `${ACTION_LABELS[kind].title} failed`, severity: "error" });
      }
    } catch (err) {
      setSnackbar({ open: true, message: `Network error: ${err instanceof Error ? err.message : "unknown"}`, severity: "error" });
    } finally {
      setActionLoading(false);
      setConfirmAction(null);
    }
  }, [confirmAction]);

  const hasActiveFilters = search !== "" || validationFilters.length > 0 || sinfoFilters.length > 0 || deviceFilters.length > 0;

  const clearFilters = () => {
    setSearch("");
    setValidationFilters([]);
    setSinfoFilters([]);
    setDeviceFilters([]);
    setPage(0);
  };

  const filteredRows = useMemo(() => {
    let result = rows;
    if (search) {
      const q = search.toLowerCase();
      result = result.filter((r) => r.rack.toLowerCase().includes(q) || r.last_job.toLowerCase().includes(q));
    }
    if (validationFilters.length > 0) {
      const set = new Set(validationFilters);
      result = result.filter((r) => set.has(nemoCategory(r.nemo)));
    }
    if (sinfoFilters.length > 0) {
      const set = new Set(sinfoFilters);
      result = result.filter((r) => {
        const states = extractSinfoStates(r.sinfo);
        return states.some((s) => set.has(s));
      });
    }
    if (deviceFilters.length > 0) {
      result = result.filter((r) => {
        const down = hasDownDevices(r.ct_down_unk);
        if (deviceFilters.includes("has_down") && down) return true;
        if (deviceFilters.includes("all_up") && !down) return true;
        return false;
      });
    }
    return result;
  }, [rows, search, validationFilters, sinfoFilters, deviceFilters]);

  const sortedRows = useMemo(() => {
    const sorted = [...filteredRows].sort((a, b) => {
      let cmp = 0;
      switch (sortCol) {
        case "rack":
          cmp = a.rack.localeCompare(b.rack, undefined, { numeric: true });
          break;
        case "nemo":
          cmp = nemoSortKey(a.nemo) - nemoSortKey(b.nemo);
          break;
        case "sinfo":
          cmp = a.sinfo.localeCompare(b.sinfo);
          break;
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return sorted;
  }, [filteredRows, sortCol, sortDir]);

  const paginatedRows = useMemo(() => {
    const start = page * rowsPerPage;
    return sortedRows.slice(start, start + rowsPerPage);
  }, [sortedRows, page, rowsPerPage]);

  const handleSort = (col: SortCol) => {
    if (selectMode) return;
    if (sortCol === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir("asc");
    }
    setPage(0);
  };

  const isJobOngoing = menuRow ? validationOngoing(menuRow.nemo) : false;
  const hasJob = menuRow ? menuRow.last_job !== "-" && menuRow.last_job !== "" : false;
  const hasDown = menuRow ? hasDownDevices(menuRow.ct_down_unk) : false;

  return (
    <Box component="section" aria-label="Rack table: sortable, filterable list of racks and validation status">
      {/* Filter bar */}
      <Paper variant="outlined" sx={{ p: 2, mb: 1.5, opacity: selectMode ? 0.55 : 1, pointerEvents: selectMode ? "none" : "auto" }}>
        <Flex gap="3" align="end" wrap="wrap">
          <div style={{ minWidth: 200 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Search
            </Text>
            <TextField
              size="small"
              placeholder="Rack or job ID..."
              value={search}
              disabled={selectMode}
              onChange={(e) => { setSearch(e.target.value); setPage(0); }}
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <Search size={16} />
                    </InputAdornment>
                  ),
                },
              }}
              fullWidth
            />
          </div>

          <div style={{ minWidth: 180 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Validation
            </Text>
            <MultiSelectCombobox
              items={VALIDATION_OPTIONS}
              value={validationFilters}
              onValueChange={(v) => { setValidationFilters(v); setPage(0); }}
              size="small"
              disabled={selectMode}
              aria-label="Filter by validation status"
            />
          </div>

          <div style={{ minWidth: 180 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Sinfo
            </Text>
            <MultiSelectCombobox
              items={SINFO_OPTIONS}
              value={sinfoFilters}
              onValueChange={(v) => { setSinfoFilters(v); setPage(0); }}
              size="small"
              disabled={selectMode}
              aria-label="Filter by sinfo state"
            />
          </div>

          <div style={{ minWidth: 160 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Nodes status
            </Text>
            <MultiSelectCombobox
              items={[
                { value: "has_down", children: "Has down nodes" },
                { value: "all_up", children: "All nodes up" },
              ]}
              value={deviceFilters}
              onValueChange={(v) => { setDeviceFilters(v); setPage(0); }}
              size="small"
              disabled={selectMode}
              aria-label="Filter by nodes status"
            />
          </div>

          {hasActiveFilters && (
            <Button kind="secondary" size="small" onClick={clearFilters} disabled={selectMode}>
              Clear filters
            </Button>
          )}
        </Flex>

        {hasActiveFilters && (
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8 }}>
            Showing {filteredRows.length} of {rows.length} racks
          </Text>
        )}
      </Paper>

      {/* Table */}
      <TableContainer component={Paper} variant="outlined">
        <Box
          sx={{
            display: "flex",
            justifyContent: "flex-end",
            alignItems: "center",
            gap: 1,
            flexWrap: "wrap",
            px: 1.5,
            py: 1,
            borderBottom: 1,
            borderColor: "divider",
            bgcolor: selectMode ? "action.selected" : "transparent",
          }}
        >
          {selectMode && selectedRacks.size > 0 && (
            <>
              <Menu anchorEl={runMenuAnchor} open={Boolean(runMenuAnchor)} onClose={() => setRunMenuAnchor(null)}>
                <MenuItem onClick={() => handleBulkRun("nemo")}>
                  <ListItemText primary="Run NEMO validation" secondary="Async, all trays ct[01–18]" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("llama")}>
                  <ListItemText primary="Run Llama" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("stress")}>
                  <ListItemText primary="Run MNUBERGEMM stress" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("nvloom")}>
                  <ListItemText primary="Run nvloom" secondary="Async · log_dir/nvloom/" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("clusterkit")}>
                  <ListItemText primary="Run ClusterKit" secondary="Async · log_dir/clusterkit/" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("llmb")}>
                  <ListItemText primary="Run NCCL Microbenchmark (LLMB)" secondary="Async · llmb-run microbenchmark_nccl" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("power_on")}>
                  <ListItemText primary="Power on" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("gpu_reset")}>
                  <ListItemText primary="GPU reset" secondary="Async" />
                </MenuItem>
                <MenuItem
                  onClick={() => {
                    setRunMenuAnchor(null);
                    setHwBulkDialogOpen(true);
                  }}
                >
                  <ListItemText primary="Hardware check" secondary="Async · choose preset or args" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("bug_report")}>
                  <ListItemText primary="Bug report" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("imex_reset")}>
                  <ListItemText primary="IMEX reset" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("imex_check")}>
                  <ListItemText primary="IMEX check" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("services_restart")}>
                  <ListItemText primary="Restart services" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("nvswitch_fix")}>
                  <ListItemText primary="NVSwitch fix" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("nvswitch_sdn")}>
                  <ListItemText primary="NVSwitch SDN reset" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("nvswitch_system")}>
                  <ListItemText primary="NVSwitch system reset" secondary="Async (per-rack host)" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("nvswitch_check")}>
                  <ListItemText primary="NVSwitch check" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("nvlmapper")}>
                  <ListItemText primary="NVLMapper" secondary="Async" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("drain")}>
                  <ListItemText primary="Slurm drain" secondary="Sync" />
                </MenuItem>
                <MenuItem onClick={() => handleBulkRun("undrain")}>
                  <ListItemText primary="Slurm undrain" secondary="Sync" />
                </MenuItem>
              </Menu>
              <MuiButton
                size="small"
                variant="contained"
                color="secondary"
                endIcon={<ChevronDown size={16} />}
                onClick={(e) => setRunMenuAnchor(e.currentTarget)}
              >
                Run
              </MuiButton>
            </>
          )}
          <MuiButton size="small" variant={selectMode ? "contained" : "outlined"} onClick={toggleSelectMode}>
            {selectMode ? "Exit selection" : "Select"}
          </MuiButton>
        </Box>
        {selectMode && (
          <Box sx={{ px: 1.5, py: 0.75, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover" }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Rack selection mode: use checkboxes. Other row actions are disabled. Run uses target{" "}
              <span style={{ fontFamily: "monospace" }}>ct[01–18]</span> per rack; jobs start in parallel.
              {selectedRacks.size > 0 ? ` Selected: ${selectedRacks.size}.` : ""}
            </Text>
          </Box>
        )}
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              {selectMode && <TableCell padding="checkbox" sx={{ width: 48 }} />}
              <TableCell>
                <TableSortLabel
                  active={sortCol === "rack"}
                  direction={sortCol === "rack" ? sortDir : "asc"}
                  onClick={() => handleSort("rack")}
                  disabled={selectMode}
                >
                  Rack
                </TableSortLabel>
              </TableCell>
              <TableCell>Comment</TableCell>
              <TableCell>
                <TableSortLabel
                  active={sortCol === "nemo"}
                  direction={sortCol === "nemo" ? sortDir : "asc"}
                  onClick={() => handleSort("nemo")}
                  disabled={selectMode}
                >
                  Validation
                </TableSortLabel>
              </TableCell>
              <TableCell>Last Job</TableCell>
              <TableCell>
                <TableSortLabel
                  active={sortCol === "sinfo"}
                  direction={sortCol === "sinfo" ? sortDir : "asc"}
                  onClick={() => handleSort("sinfo")}
                  disabled={selectMode}
                >
                  Sinfo
                </TableSortLabel>
              </TableCell>
              <TableCell>Nodes status</TableCell>
              <TableCell align="right">Results</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {paginatedRows.map((row) => {
              const rowHasDown = hasDownDevices(row.ct_down_unk);
              return (
                <TableRow key={row.rack} hover selected={selectMode && selectedRacks.has(row.rack)}>
                  {selectMode && (
                    <TableCell padding="checkbox">
                      <Checkbox
                        size="small"
                        checked={selectedRacks.has(row.rack)}
                        onChange={() => toggleRackSelected(row.rack)}
                        slotProps={{ input: { "aria-label": `Select ${row.rack}` } }}
                      />
                    </TableCell>
                  )}
                  <TableCell sx={{ fontWeight: 600 }}>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      {selectMode ? (
                        <Text kind="body/bold/md" style={{ fontFamily: "inherit" }}>
                          {row.rack}
                        </Text>
                      ) : (
                        <Link
                          component="button"
                          underline="hover"
                          color="primary"
                          sx={{ fontWeight: 600, fontSize: "inherit" }}
                          onClick={() => onRackSelect?.(row.rack)}
                        >
                          {row.rack}
                        </Link>
                      )}
                      <Box sx={{ opacity: selectMode ? 0.35 : 1, pointerEvents: selectMode ? "none" : "auto" }}>
                        <RackTableCommandIndicator rack={row.rack} />
                      </Box>
                    </Box>
                  </TableCell>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      {comments[row.rack] ? (
                        <Tooltip title={<span style={{ whiteSpace: "pre-line" }}>{comments[row.rack]}</span>} placement="bottom-start" arrow>
                          <span style={{ color: "var(--text-color-secondary)", maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "inline-block", fontSize: 14 }}>
                            {comments[row.rack].length > 20 ? comments[row.rack].slice(0, 20) + "..." : comments[row.rack]}
                          </span>
                        </Tooltip>
                      ) : (
                        <span style={{ color: "var(--text-color-subtle)", fontSize: 14 }}>—</span>
                      )}
                      <IconButton size="small" disabled={selectMode} onClick={(e) => openMenu(e, "comment", row)} sx={{ flexShrink: 0 }} aria-label={`Comment actions for ${row.rack}`}>
                        <Ellipsis size={16} />
                      </IconButton>
                    </Box>
                  </TableCell>
                  <TableCell>
                    <ValidationTag row={row} />
                  </TableCell>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>
                        {row.last_job}
                      </Text>
                      {row.last_job !== "-" && row.last_job !== "" && (
                        <IconButton size="small" disabled={selectMode} onClick={(e) => openMenu(e, "job", row)} sx={{ flexShrink: 0 }} aria-label={`Job actions for ${row.rack}`}>
                          <Ellipsis size={16} />
                        </IconButton>
                      )}
                    </Box>
                  </TableCell>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      <SinfoTags sinfo={row.sinfo} />
                      <IconButton size="small" disabled={selectMode} onClick={(e) => openMenu(e, "sinfo", row)} sx={{ ml: "auto", flexShrink: 0 }} aria-label={`Node state actions for ${row.rack}`}>
                        <Ellipsis size={16} />
                      </IconButton>
                    </Box>
                  </TableCell>
                  <TableCell sx={{ verticalAlign: "top" }}>
                    <NodeStatusTags
                      ct={row.ct_down_unk}
                      rowHasDown={rowHasDown}
                      menuDisabled={selectMode}
                      onOpenDeviceMenu={(e) => openMenu(e, "device", row)}
                    />
                  </TableCell>
                  <TableCell align="right">
                    <Box sx={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 0.5, flexWrap: "wrap" }}>
                      {validationOngoing(row.nemo) ? (
                        <MuiButton size="small" variant="outlined" disabled={selectMode} onClick={() => startTailForRow(row)}>
                          Tail Log
                        </MuiButton>
                      ) : (
                        <MuiButton size="small" variant="outlined" disabled={selectMode} onClick={() => void openLogForRow(row)}>
                          Open Log
                        </MuiButton>
                      )}
                      <Tooltip title="NCCL / NEMO charts and Grafana (parser.sh --json)">
                        <IconButton
                          size="small"
                          disabled={selectMode}
                          onClick={() => openParserForRow(row, 1)}
                          aria-label="Open parser results"
                        >
                          <LineChart size={18} />
                        </IconButton>
                      </Tooltip>
                      <GrafanaLinksMenu rack={row.rack} disabled={selectMode} />
                    </Box>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        <TablePagination
          component="div"
          count={sortedRows.length}
          page={page}
          onPageChange={(_, newPage) => {
            if (!selectMode) setPage(newPage);
          }}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => {
            if (selectMode) return;
            setRowsPerPage(parseInt(e.target.value, 10));
            setPage(0);
          }}
          rowsPerPageOptions={[25, 50, 100]}
          sx={selectMode ? { pointerEvents: "none", opacity: 0.5 } : undefined}
        />
      </TableContainer>

      {/* Sinfo actions menu */}
      <Menu
        anchorEl={menuKind === "sinfo" ? menuAnchor : null}
        open={menuKind === "sinfo" && !!menuAnchor}
        onClose={closeMenu}
        transformOrigin={{ horizontal: "right", vertical: "top" }}
        anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      >
        <MenuItem onClick={handleCheckSinfo}>
          <ListItemIcon><Server size={18} /></ListItemIcon>
          <ListItemText>Check sinfo</ListItemText>
        </MenuItem>
        <MenuItem onClick={() => requestConfirm("drain")}>
          <ListItemIcon><ArrowDownToLine size={18} /></ListItemIcon>
          <ListItemText>Drain nodes</ListItemText>
        </MenuItem>
        <MenuItem onClick={() => requestConfirm("undrain")}>
          <ListItemIcon><ArrowUpFromLine size={18} /></ListItemIcon>
          <ListItemText>Undrain nodes</ListItemText>
        </MenuItem>
      </Menu>

      {/* Job actions menu */}
      <Menu
        anchorEl={menuKind === "job" ? menuAnchor : null}
        open={menuKind === "job" && !!menuAnchor}
        onClose={closeMenu}
        transformOrigin={{ horizontal: "right", vertical: "top" }}
        anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      >
        {isJobOngoing && (
          <MenuItem onClick={handleTailLog}>
            <ListItemIcon><ScrollText size={18} /></ListItemIcon>
            <ListItemText>Tail log</ListItemText>
          </MenuItem>
        )}
        {!isJobOngoing && (
          <MenuItem onClick={() => void handleOpenLog()}>
            <ListItemIcon><FileText size={18} /></ListItemIcon>
            <ListItemText>Open log</ListItemText>
          </MenuItem>
        )}
        {hasJob && isJobOngoing && (
          <MenuItem onClick={() => requestConfirm("cancel")}>
            <ListItemIcon><XCircle size={18} /></ListItemIcon>
            <ListItemText>Cancel job {menuRow?.last_job}</ListItemText>
          </MenuItem>
        )}
      </Menu>

      {/* Device actions menu */}
      <Menu
        anchorEl={menuKind === "device" ? menuAnchor : null}
        open={menuKind === "device" && !!menuAnchor}
        onClose={closeMenu}
        transformOrigin={{ horizontal: "right", vertical: "top" }}
        anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      >
        {hasDown && (
          <MenuItem onClick={() => requestConfirm("power_up")}>
            <ListItemIcon><Power size={18} /></ListItemIcon>
            <ListItemText>Power up down nodes</ListItemText>
          </MenuItem>
        )}
      </Menu>

      {/* Comment actions menu */}
      <Menu
        anchorEl={menuKind === "comment" ? menuAnchor : null}
        open={menuKind === "comment" && !!menuAnchor}
        onClose={closeMenu}
        transformOrigin={{ horizontal: "right", vertical: "top" }}
        anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      >
        {menuRow && !comments[menuRow.rack] && (
          <MenuItem onClick={handleAddEditComment}>
            <ListItemIcon><MessageSquarePlus size={18} /></ListItemIcon>
            <ListItemText>Add comment</ListItemText>
          </MenuItem>
        )}
        {menuRow && comments[menuRow.rack] && (
          <MenuItem onClick={handleAddEditComment}>
            <ListItemIcon><Pencil size={18} /></ListItemIcon>
            <ListItemText>Edit comment</ListItemText>
          </MenuItem>
        )}
        {menuRow && comments[menuRow.rack] && (
          <MenuItem onClick={handleDeleteComment}>
            <ListItemIcon><Trash2 size={18} /></ListItemIcon>
            <ListItemText>Delete comment</ListItemText>
          </MenuItem>
        )}
      </Menu>

      {/* Comment edit dialog */}
      <Dialog open={commentDialogOpen} onClose={() => !commentSaving && setCommentDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{comments[commentDialogRack] ? "Edit Comment" : "Add Comment"} — {commentDialogRack}</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            multiline
            minRows={3}
            maxRows={10}
            fullWidth
            placeholder="Enter comment for this rack..."
            value={commentDialogText}
            onChange={(e) => setCommentDialogText(e.target.value)}
            sx={{ mt: 1 }}
          />
        </DialogContent>
        <DialogActions>
          <MuiButton variant="outlined" onClick={() => setCommentDialogOpen(false)} disabled={commentSaving}>
            Cancel
          </MuiButton>
          <MuiButton
            onClick={handleSaveComment}
            variant="contained"
            disabled={commentSaving}
            startIcon={commentSaving ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            {commentSaving ? "Saving..." : "Save"}
          </MuiButton>
        </DialogActions>
      </Dialog>

      {/* Confirmation dialog */}
      <Dialog open={!!confirmAction} onClose={() => !actionLoading && setConfirmAction(null)}>
        <DialogTitle>{confirmAction ? ACTION_LABELS[confirmAction.kind].title : ""}</DialogTitle>
        <DialogContent>
          <DialogContentText>
            {confirmAction
              ? ACTION_LABELS[confirmAction.kind].description(confirmAction.rack, confirmAction.jobId)
              : ""}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <MuiButton variant="outlined" onClick={() => setConfirmAction(null)} disabled={actionLoading}>
            Cancel
          </MuiButton>
          <MuiButton
            onClick={executeAction}
            variant="contained"
            color={confirmAction?.kind === "drain" || confirmAction?.kind === "cancel" ? "error" : "primary"}
            disabled={actionLoading}
            startIcon={actionLoading ? <CircularProgress size={16} color="inherit" /> : undefined}
          >
            {actionLoading ? "Running..." : confirmAction ? ACTION_LABELS[confirmAction.kind].confirm : ""}
          </MuiButton>
        </DialogActions>
      </Dialog>

      {/* Feedback snackbar */}
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

      {/* Parser results dialog */}
      {parserOpen ? (
        <Suspense fallback={null}>
          <ParserResultsDialog
            open
            onClose={() => {
              setParserOpen(false);
              setParserInitialTab(0);
            }}
            rack={parserRack}
            jobId={parserJobId}
            logDir={logDir}
            initialTabIndex={parserInitialTab}
          />
        </Suspense>
      ) : null}

      <HwCheckRunDialog
        open={hwBulkDialogOpen}
        onClose={() => setHwBulkDialogOpen(false)}
        title="Hardware check (bulk)"
        subtitle={
          selectedRackList.length
            ? `${selectedRackList.length} rack(s): ${selectedRackList.join(", ")} · ct[01–18] per rack`
            : undefined
        }
        onConfirm={(opts) => runHwCheckBulk(opts)}
      />
    </Box>
  );
}
