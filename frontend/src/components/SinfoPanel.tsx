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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import LinearProgress from "@mui/material/LinearProgress";
import { Text, Button } from "@nvidia/foundations-react-core";
import { fetchSlurmJson } from "../lib/slurmHelpers";
import { parseSinfoRStdout } from "../lib/parseSinfoRStdout";
import { slurmReasonTone, slurmToneColor } from "../lib/slurmReasonTone";
import { useSortedPagedRows } from "../hooks/useSortedPagedRows";
import { TablePager } from "./TablePager";
import { SortableHeaderCell } from "./SortableHeaderCell";

const NVIDIA_GREEN = "#76B900";

/** Stable accessor: sinfo rows are carried as {r, i} so the original index survives filtering. */
const cellAt = (entry: { r: string[] }, col: number) => entry.r[col] ?? "";

interface SinfoPanelProps {
  active: boolean;
  tableMaxHeight?: string | number;
  /** Optional seed for filter when opening from rack context (toolbar: empty). */
  searchSeed?: string;
}

const EMPTY_ROWS: string[][] = [];

export function SinfoPanel({ active, tableMaxHeight = 480, searchSeed = "" }: SinfoPanelProps) {
  const [loading, setLoading] = useState(false);
  const [cmd, setCmd] = useState("");
  const [bannerErr, setBannerErr] = useState("");
  const [stderrNote, setStderrNote] = useState("");
  const [stdout, setStdout] = useState("");
  const [parsed, setParsed] = useState<ReturnType<typeof parseSinfoRStdout> | null>(null);
  const [filter, setFilter] = useState("");
  const prevActive = useRef(false);

  useEffect(() => {
    if (active && !prevActive.current) {
      setFilter(searchSeed.trim().toLowerCase());
    }
    prevActive.current = active;
  }, [active, searchSeed]);

  const resetBody = useCallback(() => {
    setBannerErr("");
    setStderrNote("");
    setStdout("");
    setParsed(null);
    setCmd("");
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    resetBody();
    try {
      const { res, data } = await fetchSlurmJson("/api/scheduler-node-reasons");
      setCmd(data.command_display || "");
      if (!res.ok) {
        setBannerErr(data.error || `HTTP ${res.status}`);
        const bits: string[] = [];
        if (data.stdout) bits.push(String(data.stdout));
        if (data.stderr) bits.push(`stderr:\n${String(data.stderr)}`);
        setStdout(bits.join("\n\n") || "");
        setParsed({ headers: [], rows: [], useRaw: true });
        return;
      }
      if (data.ok === false) {
        setBannerErr(
          [data.error || "sinfo -R --long failed", data.stderr ? `\n${String(data.stderr)}` : "", data.returncode != null ? `\n(exit ${data.returncode})` : ""]
            .filter(Boolean)
            .join("")
        );
      }
      const out = data.stdout != null ? String(data.stdout) : "";
      setStdout(out);
      setParsed(parseSinfoRStdout(out));
      if (data.ok && data.stderr && String(data.stderr).trim()) {
        setStderrNote(`stderr:\n${String(data.stderr).trim()}`);
      }
    } catch (e) {
      setBannerErr(String(e));
      setParsed({ headers: [], rows: [], useRaw: true });
    } finally {
      setLoading(false);
    }
  }, [resetBody]);

  useEffect(() => {
    if (!active) return;
    void load();
  }, [active, load]);

  const headers = parsed && !parsed.useRaw && parsed.headers.length ? parsed.headers : [];
  // EMPTY_ROWS, not a literal `[]`: a fresh array each render re-runs the filter memo below.
  const rows = parsed && !parsed.useRaw && parsed.headers.length ? parsed.rows : EMPTY_ROWS;
  const showRaw = !parsed || parsed.useRaw || !headers.length;
  const toneCols = headers
    .map((h, i) => (/^(STATE|REASON)$/i.test(h) ? i : -1))
    .filter((i) => i >= 0);

  // Filter, rather than render every row with a `show` flag. The flag version kept all of a
  // 2000-node sinfo in the DOM and, once paged, would have counted hidden rows in the range text.
  const matchedRows = useMemo(() => {
    const all = rows.map((r, i) => ({ r, i }));
    const q = filter.trim().toLowerCase();
    if (!q) return all;
    return all.filter(({ r }) => r.join(" ").toLowerCase().includes(q));
  }, [rows, filter]);

  const rowsPage = useSortedPagedRows(matchedRows, cellAt);

  return (
    <Paper variant="outlined" sx={{ borderLeft: `4px solid ${NVIDIA_GREEN}`, overflow: "hidden" }}>
      <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover", display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN }}>
          sinfo -R --long
        </Text>
        <Button size="small" kind="secondary" onClick={() => void load()} disabled={loading || !active}>
          Refresh
        </Button>
        {!showRaw && headers.length > 0 && (
          <TextField
            size="small"
            label="Filter rows"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            sx={{ minWidth: 200, flex: 1 }}
          />
        )}
        {loading && (
          <Box sx={{ flex: 1, minWidth: 120 }}>
            <LinearProgress />
          </Box>
        )}
      </Box>

      {cmd && (
        <Text kind="body/regular/sm" style={{ display: "block", paddingLeft: 16, paddingRight: 16, paddingTop: 8, fontFamily: "monospace", wordBreak: "break-all", color: "var(--text-color-secondary)" }}>
          {cmd}
        </Text>
      )}

      {bannerErr && (
        <Text kind="body/regular/md" style={{ display: "block", paddingLeft: 16, paddingRight: 16, paddingTop: 8, whiteSpace: "pre-wrap", color: "var(--text-color-error, #f44336)" }}>
          {bannerErr}
        </Text>
      )}
      {stderrNote && (
        <Text kind="body/regular/md" style={{ display: "block", paddingLeft: 16, paddingRight: 16, paddingTop: 8, whiteSpace: "pre-wrap", color: "#ed6c02" }}>
          {stderrNote}
        </Text>
      )}

      {!showRaw && headers.length > 0 && (
        <TableContainer sx={{ maxHeight: tableMaxHeight }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {headers.map((h, ci) => (
                  <SortableHeaderCell
                    key={h}
                    label={h}
                    col={ci}
                    sortCol={rowsPage.sortCol}
                    sortDir={rowsPage.sortDir}
                    onToggle={rowsPage.toggleSort}
                  />
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {rowsPage.visible.map(({ r, i }) => (
                <TableRow key={i} hover sx={{ "&:nth-of-type(even)": { bgcolor: "action.hover" } }}>
                  {headers.map((_, ci) => {
                    const value = r[ci] ?? "";
                    const tone = toneCols.includes(ci) ? slurmReasonTone(value) : "none";
                    const toneColor = slurmToneColor(tone);
                    return (
                      <TableCell
                        key={ci}
                        title={value}
                        sx={{
                          fontFamily: "monospace",
                          fontSize: 11,
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          maxWidth: 360,
                          color: toneColor,
                          fontWeight: toneColor ? 600 : undefined,
                        }}
                      >
                        {value}
                      </TableCell>
                    );
                  })}
                </TableRow>
              ))}
              {!rowsPage.total && (
                <TableRow>
                  <TableCell colSpan={Math.max(1, headers.length)}>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      No rows match “{filter.trim()}”.
                    </Text>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {!showRaw && headers.length > 0 && (
        <Box sx={{ px: 2, pb: 1 }}>
          <TablePager {...rowsPage} noun="nodes" compact />
        </Box>
      )}

      {showRaw && !loading && (
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 2,
            maxHeight: tableMaxHeight,
            overflow: "auto",
            fontSize: 12,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {stdout || "(no output)"}
        </Box>
      )}
    </Paper>
  );
}
