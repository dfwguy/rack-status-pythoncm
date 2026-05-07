import { useState, useMemo, useCallback } from "react";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
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
import { Tag, Text, Flex, Combobox, Button } from "@nvidia/foundations-react-core";
import Tooltip from "@mui/material/Tooltip";
import {
  CircleCheck,
  Cpu,
  Shuffle,
  AlertTriangle,
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
} from "lucide-react";
import { ParserResultsDialog } from "./ParserResultsDialog";
import type { RackRow } from "../types/api";

const ICON_SIZE = 14;

interface RackTableProps {
  rows: RackRow[];
  comments: Record<string, string>;
  logDir?: string;
  onRackSelect?: (rack: string) => void;
  onCommentChange?: (rack: string, comment: string | null) => void;
}

type SortCol = "rack" | "nemo" | "sinfo";
type SortDir = "asc" | "desc";
type ActionKind = "drain" | "undrain" | "cancel" | "power_up";
type MenuKind = "sinfo" | "job" | "device" | "comment";

const VALIDATION_OPTIONS = ["done", "fail", "ongoing", "pending"];
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

function nemoCategory(nemo: string): string {
  if (nemo === "done") return "done";
  if (nemo === "fail") return "fail";
  if (nemo.startsWith("ongoing")) return "ongoing";
  return "pending";
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
    case "drain": return <AlertTriangle size={ICON_SIZE} />;
    case "down": return <XCircle size={ICON_SIZE} />;
    case "comp": return <Monitor size={ICON_SIZE} />;
    default: return null;
  }
}

function ValidationTag({ nemo }: { nemo: string }) {
  const color = validationTagColor(nemoCategory(nemo));
  return <Tag color={color} kind="solid" readOnly density="compact">{nemo}</Tag>;
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

function parseDeviceSegments(ct: string): { up: string[]; down: string[] } {
  if (ct === "-" || ct === "?") return { up: [], down: [] };
  const segments = ct.split(";").map((s) => s.trim()).filter(Boolean);
  const up: string[] = [];
  const down: string[] = [];
  for (const seg of segments) {
    if (seg.startsWith("UP:")) {
      up.push(seg.slice(3));
    } else if (seg.startsWith("DN:")) {
      down.push(seg.slice(3));
    } else if (seg.startsWith("INST:")) {
      down.push(`INST ${seg.slice(5)}`);
    } else {
      down.push(seg);
    }
  }
  return { up, down };
}

function DeviceUpTags({ ct }: { ct: string }) {
  const { up } = parseDeviceSegments(ct);
  if (ct === "-" || ct === "?") {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{ct}</Text>;
  }
  if (up.length === 0) {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>-</Text>;
  }
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
      {up.map((range, i) => (
        <Tag key={i} color="green" kind="solid" readOnly density="compact">{range}</Tag>
      ))}
    </Box>
  );
}

function DeviceDownTags({ ct }: { ct: string }) {
  const { down } = parseDeviceSegments(ct);
  if (ct === "-" || ct === "?") {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{ct}</Text>;
  }
  if (down.length === 0) {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>-</Text>;
  }
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
      {down.map((seg, i) => {
        const isInst = seg.startsWith("INST ");
        return (
          <Tag key={i} color={isInst ? "blue" : "red"} kind="solid" readOnly density="compact">{seg}</Tag>
        );
      })}
    </Box>
  );
}

export function RackTable({ rows, comments, logDir, onRackSelect, onCommentChange }: RackTableProps) {
  const [sortCol, setSortCol] = useState<SortCol>("rack");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);

  const [search, setSearch] = useState("");
  const [validationFilters, setValidationFilters] = useState<string[]>([]);
  const [sinfoFilters, setSinfoFilters] = useState<string[]>([]);
  const [deviceFilters, setDeviceFilters] = useState<string[]>([]);

  // Parser results dialog
  const [parserOpen, setParserOpen] = useState(false);
  const [parserRack, setParserRack] = useState("");
  const [parserJobId, setParserJobId] = useState<string | undefined>();

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
  const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" }>({
    open: false, message: "", severity: "success",
  });

  const openMenu = useCallback((event: React.MouseEvent<HTMLElement>, kind: MenuKind, row: RackRow) => {
    setMenuAnchor(event.currentTarget);
    setMenuKind(kind);
    setMenuRow(row);
  }, []);

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
    window.open("/api/slurm-sinfo-r", "_blank");
  }, [closeMenu]);

  const handleOpenLog = useCallback(() => {
    if (!menuRow) return;
    setParserRack(menuRow.rack);
    setParserJobId(menuRow.last_job !== "-" ? menuRow.last_job : undefined);
    setParserOpen(true);
    closeMenu();
  }, [menuRow, closeMenu]);

  const handleTailLog = useCallback(() => {
    if (!menuRow) return;
    setParserRack(menuRow.rack);
    setParserJobId(menuRow.last_job !== "-" ? menuRow.last_job : undefined);
    setParserOpen(true);
    closeMenu();
  }, [menuRow, closeMenu]);

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
      await fetch(`/api/comments/${encodeURIComponent(rack)}`, { method: "DELETE" });
      onCommentChange?.(rack, null);
      setSnackbar({ open: true, message: `Comment removed for ${rack}`, severity: "success" });
    } catch {
      setSnackbar({ open: true, message: "Failed to delete comment", severity: "error" });
    }
  }, [menuRow, closeMenu, onCommentChange]);

  const handleSaveComment = useCallback(async () => {
    setCommentSaving(true);
    try {
      const res = await fetch(`/api/comments/${encodeURIComponent(commentDialogRack)}`, {
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
          url = "/api/slurm-drain";
          body = { rack };
          break;
        case "undrain":
          url = "/api/slurm-undrain";
          body = { rack };
          break;
        case "cancel":
          url = "/api/slurm-scancel";
          body = { job_id: jobId ?? "", rack };
          break;
        case "power_up":
          url = "/api/power-reset";
          body = { rack };
          break;
      }

      const res = await fetch(url, {
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
    if (sortCol === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir("asc");
    }
    setPage(0);
  };

  const isJobOngoing = menuRow ? nemoCategory(menuRow.nemo) === "ongoing" : false;
  const isJobDone = menuRow ? nemoCategory(menuRow.nemo) === "done" : false;
  const hasJob = menuRow ? menuRow.last_job !== "-" && menuRow.last_job !== "" : false;
  const hasDown = menuRow ? hasDownDevices(menuRow.ct_down_unk) : false;

  return (
    <Box>
      {/* Filter bar */}
      <Paper variant="outlined" sx={{ p: 2, mb: 1.5 }}>
        <Flex gap="3" align="end" wrap="wrap">
          <div style={{ minWidth: 200 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Search
            </Text>
            <TextField
              size="small"
              placeholder="Rack or job ID..."
              value={search}
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
            <Combobox
              items={VALIDATION_OPTIONS}
              multiple
              selectedValue={validationFilters}
              onSelectedValueChange={(v) => { setValidationFilters(v as string[]); setPage(0); }}
              size="small"
              aria-label="Filter by validation status"
            />
          </div>

          <div style={{ minWidth: 180 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Sinfo
            </Text>
            <Combobox
              items={SINFO_OPTIONS}
              multiple
              selectedValue={sinfoFilters}
              onSelectedValueChange={(v) => { setSinfoFilters(v as string[]); setPage(0); }}
              size="small"
              aria-label="Filter by sinfo state"
            />
          </div>

          <div style={{ minWidth: 160 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Devices
            </Text>
            <Combobox
              items={[
                { value: "has_down", children: "Has down nodes" },
                { value: "all_up", children: "All nodes up" },
              ]}
              multiple
              selectedValue={deviceFilters}
              onSelectedValueChange={(v) => { setDeviceFilters(v as string[]); setPage(0); }}
              size="small"
              aria-label="Filter by device status"
            />
          </div>

          {hasActiveFilters && (
            <Button kind="tertiary" size="small" onClick={clearFilters}>
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
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell>
                <TableSortLabel
                  active={sortCol === "rack"}
                  direction={sortCol === "rack" ? sortDir : "asc"}
                  onClick={() => handleSort("rack")}
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
                >
                  Sinfo
                </TableSortLabel>
              </TableCell>
              <TableCell>Devices Up</TableCell>
              <TableCell>Devices Down</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {paginatedRows.map((row) => {
              const rowHasDown = hasDownDevices(row.ct_down_unk);
              return (
                <TableRow key={row.rack} hover>
                  <TableCell sx={{ fontWeight: 600 }}>
                    <Link
                      component="button"
                      underline="hover"
                      color="primary"
                      sx={{ fontWeight: 600, fontSize: "inherit" }}
                      onClick={() => onRackSelect?.(row.rack)}
                    >
                      {row.rack}
                    </Link>
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
                      <IconButton size="small" onClick={(e) => openMenu(e, "comment", row)} sx={{ flexShrink: 0 }}>
                        <Ellipsis size={16} />
                      </IconButton>
                    </Box>
                  </TableCell>
                  <TableCell>
                    <ValidationTag nemo={row.nemo} />
                  </TableCell>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>
                        {row.last_job}
                      </Text>
                      {row.last_job !== "-" && row.last_job !== "" && (
                        <IconButton size="small" onClick={(e) => openMenu(e, "job", row)} sx={{ flexShrink: 0 }}>
                          <Ellipsis size={16} />
                        </IconButton>
                      )}
                    </Box>
                  </TableCell>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      <SinfoTags sinfo={row.sinfo} />
                      <IconButton size="small" onClick={(e) => openMenu(e, "sinfo", row)} sx={{ ml: "auto", flexShrink: 0 }}>
                        <Ellipsis size={16} />
                      </IconButton>
                    </Box>
                  </TableCell>
                  <TableCell>
                    <DeviceUpTags ct={row.ct_down_unk} />
                  </TableCell>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      <DeviceDownTags ct={row.ct_down_unk} />
                      {rowHasDown && (
                        <IconButton size="small" onClick={(e) => openMenu(e, "device", row)} sx={{ ml: "auto", flexShrink: 0 }}>
                          <Ellipsis size={16} />
                        </IconButton>
                      )}
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
          onPageChange={(_, newPage) => setPage(newPage)}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => {
            setRowsPerPage(parseInt(e.target.value, 10));
            setPage(0);
          }}
          rowsPerPageOptions={[25, 50, 100]}
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
        {hasJob && isJobOngoing && (
          <MenuItem onClick={handleTailLog}>
            <ListItemIcon><ScrollText size={18} /></ListItemIcon>
            <ListItemText>Tail log</ListItemText>
          </MenuItem>
        )}
        {hasJob && isJobDone && (
          <MenuItem onClick={handleOpenLog}>
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
        {hasJob && !isJobOngoing && !isJobDone && (
          <MenuItem disabled>
            <ListItemText>No actions available</ListItemText>
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
          <MuiButton onClick={() => setCommentDialogOpen(false)} disabled={commentSaving}>
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
          <MuiButton onClick={() => setConfirmAction(null)} disabled={actionLoading}>
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

      {/* Parser results dialog */}
      <ParserResultsDialog
        open={parserOpen}
        onClose={() => setParserOpen(false)}
        rack={parserRack}
        jobId={parserJobId}
        logDir={logDir}
      />
    </Box>
  );
}
