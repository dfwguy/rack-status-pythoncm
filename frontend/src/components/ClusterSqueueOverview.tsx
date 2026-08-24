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
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import { Skeleton, Text } from "@nvidia/foundations-react-core";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import { parseSqueueStdout } from "../lib/parseSqueueStdout";
import { usePagedRows } from "../hooks/usePagedRows";
import { TablePager } from "./TablePager";
import type { ClusterSqueueBlock } from "../types/api";

const NVIDIA_GREEN = "#76B900";

interface ClusterSqueueOverviewProps {
  squeue: ClusterSqueueBlock | null | undefined;
  loading?: boolean;
  failed?: boolean;
}

export function ClusterSqueueOverview({
  squeue,
  loading = false,
  failed = false,
}: ClusterSqueueOverviewProps) {
  if (loading) {
    return (
      <Paper
        variant="outlined"
        sx={{
          borderLeft: `4px solid ${NVIDIA_GREEN}`,
          overflow: "hidden",
        }}
      >
        <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover" }}>
          <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN, display: "block" }}>
            Active jobs
          </Text>
        </Box>
        <Box sx={{ p: 2 }}>
          <Skeleton style={{ width: "100%", height: 120 }} aria-hidden />
        </Box>
      </Paper>
    );
  }

  if (failed || !squeue) {
    return (
      <Paper
        variant="outlined"
        sx={{
          borderLeft: `4px solid ${NVIDIA_GREEN}`,
          overflow: "hidden",
        }}
      >
        <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover" }}>
          <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN, display: "block" }}>
            Active jobs
          </Text>
          <Text kind="body/regular/md" style={{ display: "block", marginTop: 8, color: "var(--text-color-error, #f44336)" }}>
            {squeue?.error || "Queue details unavailable"}
          </Text>
        </Box>
      </Paper>
    );
  }

  const raw = squeue.stdout || "";
  const parsed = parseSqueueStdout(raw);

  return (
    <Paper
      variant="outlined"
      sx={{
        borderLeft: `4px solid ${NVIDIA_GREEN}`,
        overflow: "hidden",
      }}
    >
      <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover" }}>
        <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN, display: "block" }}>
          Active jobs
        </Text>
        {squeue.command_display && (
          <Text kind="body/regular/sm" style={{ display: "block", fontFamily: "monospace", marginTop: 4, wordBreak: "break-all", color: "var(--text-color-secondary)" }}>
            {squeue.command_display}
          </Text>
        )}
        {!squeue.ok && squeue.error && (
          <Text kind="body/regular/md" style={{ display: "block", marginTop: 8, color: "var(--text-color-error, #f44336)" }}>
            {squeue.error}
          </Text>
        )}
      </Box>
      <Box sx={{ p: 0 }}>
        {parsed.useRaw || !parsed.headers.length ? (
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 2,
              maxHeight: 360,
              overflow: "auto",
              fontSize: 12,
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {raw || "(empty)"}
          </Box>
        ) : (
          <SqueueRowsTable headers={parsed.headers} rows={parsed.rows} />
        )}
      </Box>
    </Paper>
  );
}

/**
 * The queue rows, paged.
 *
 * A separate component only because the parent early-returns for its loading and failed states,
 * and a hook cannot sit after a conditional return.
 */
function SqueueRowsTable({ headers, rows }: { headers: string[]; rows: string[][] }) {
  const rowsPage = usePagedRows(rows, 10);
  return (
    <>
      <TableContainer sx={{ maxHeight: 360 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              {headers.map((h) => (
                <TableCell
                  key={h}
                  sx={{
                    fontWeight: 700,
                    bgcolor: "action.hover",
                    color: NVIDIA_GREEN,
                    whiteSpace: "nowrap",
                  }}
                >
                  {h}
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {rowsPage.visible.map((cells, ri) => (
              <TableRow key={ri} hover sx={{ "&:nth-of-type(even)": { bgcolor: "action.hover" } }}>
                {headers.map((_, ci) => (
                  <TableCell key={ci} sx={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                    {cells[ci] ?? ""}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Box sx={{ px: 2, pb: 1 }}>
        <TablePager {...rowsPage} noun="jobs" compact />
      </Box>
    </>
  );
}
