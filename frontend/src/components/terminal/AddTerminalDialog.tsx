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
// AddTerminalDialog — search for a compute node (or the head node) and open a shell on it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Node names are not something the backend hands out as a list: they are derived in the SPA from
// the rack rows by parseNodesFromRack, which is the same helper Nodes, Rack Detail and the scope
// tree use. Reusing it matters more than convenience -- rack id and real compute hostname differ
// per cluster, and a second derivation here would be a second place to get that wrong.
//
// Fetched once when the dialog opens rather than polled: this is a picker, and useRackStatus's
// 15-second poll would keep re-rendering a list the operator is trying to click.
import { useEffect, useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Box from "@mui/material/Box";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import CircularProgress from "@mui/material/CircularProgress";
import MuiButton from "@mui/material/Button";
import { Banner, Button, Tag, Text, TextInput } from "@nvidia/foundations-react-core";
import { Server, TerminalSquare } from "lucide-react";
import { parseNodesFromRack } from "../../lib/parseNodesFromRack";
import type { NodeInfo } from "../rack/NodeSelector";
import type { RackStatusResponse, RackRow } from "../../types/api";
import type { NvisNamingSchema } from "../../types/naming";

/** How many rows to render at once. A 2000-node fleet would otherwise build 2000 DOM nodes. */
const MAX_ROWS = 60;

export interface TerminalTarget {
  target: string;
  mode: "node" | "headnode";
  label: string;
}

interface AddTerminalDialogProps {
  open: boolean;
  onClose: () => void;
  onAdd: (target: TerminalTarget) => void;
  logDir?: string;
  traysPerRack: number;
}

interface Row extends NodeInfo {
  rack: string;
}

export function AddTerminalDialog({
  open,
  onClose,
  onAdd,
  logDir,
  traysPerRack,
}: AddTerminalDialogProps) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError("");
    void (async () => {
      try {
        const params = new URLSearchParams();
        if (logDir) params.set("log_dir", logDir);
        const res = await fetch(`/api/status?${params}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: RackStatusResponse = await res.json();
        if (cancelled) return;
        const naming = data.naming as NvisNamingSchema | undefined;
        const physicalRf0 = data.physical_rf0 ?? {};
        const out: Row[] = [];
        for (const r of (data.rows ?? []) as RackRow[]) {
          for (const n of parseNodesFromRack(r.rack, r.ct_down_unk, physicalRf0, traysPerRack, naming)) {
            out.push({ ...n, rack: r.rack });
          }
        }
        setRows(out);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, logDir, traysPerRack]);

  // Reset the search each time it opens; a stale filter from last time hides everything.
  useEffect(() => {
    if (open) setQuery("");
  }, [open]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = rows ?? [];
    if (!q) return all.slice(0, MAX_ROWS);
    return all.filter((r) => r.id.toLowerCase().includes(q) || r.rack.toLowerCase().includes(q))
      .slice(0, MAX_ROWS);
  }, [rows, query]);

  const total = rows?.length ?? 0;
  const shown = matches.length;

  const pick = (t: TerminalTarget) => {
    onAdd(t);
    onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, py: 1.5 }}>
        <TerminalSquare size={20} />
        <Text kind="body/bold/md">Add a terminal</Text>
      </DialogTitle>

      <DialogContent sx={{ pt: 1 }}>
        {error && (
          <Banner status="error" kind="inline">
            Could not load the node list — {error}
          </Banner>
        )}

        {/* The head node is offered explicitly rather than being buried in the search results: it
            is not a compute node, it connects by a different mode, and it is the most common
            target for PythonCM inventory and direct-execution work. */}
        <Box sx={{ mb: 2 }}>
          <Button
            kind="secondary"
            size="small"
            onClick={() => pick({ target: "headnode", mode: "headnode", label: "Head Node" })}
          >
            <Server size={14} style={{ marginRight: 6 }} />
            Head node
          </Button>
        </Box>

        <TextInput
          value={query}
          onChange={(e) => setQuery((e.target as HTMLInputElement).value)}
          placeholder="Search compute nodes by name or rack…"
          size="small"
          aria-label="Search compute nodes"
        />

        {rows === null && !error ? (
          <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 3 }}>
            <CircularProgress size={16} />
            <Text kind="body/regular/sm">Loading nodes…</Text>
          </Box>
        ) : (
          <>
            <Text
              kind="body/regular/sm"
              style={{ display: "block", marginTop: 8, color: "var(--text-color-secondary)" }}
            >
              {total === 0
                ? "No nodes found for the active cluster."
                : shown < total
                  ? `Showing ${shown} of ${total} nodes — narrow the search to see more.`
                  : `${total} node${total === 1 ? "" : "s"}`}
            </Text>

            <List dense sx={{ maxHeight: 320, overflow: "auto", mt: 1 }}>
              {matches.map((r) => (
                <ListItemButton
                  key={r.id}
                  onClick={() => pick({ target: r.id, mode: "node", label: r.id })}
                  sx={{ display: "flex", gap: 1, alignItems: "center" }}
                >
                  <Text kind="mono/sm" style={{ flex: 1 }}>
                    {r.id}
                  </Text>
                  {/* A shell on a down node will fail to connect; say so before the click, but do
                      not block it -- reaching a sick node is often exactly the point. */}
                  <Tag
                    color={r.status === "up" ? "green" : r.status === "down" ? "red" : "gray"}
                    kind="outline"
                    readOnly
                    density="compact"
                  >
                    {r.status}
                  </Tag>
                </ListItemButton>
              ))}
            </List>
          </>
        )}
      </DialogContent>

      <DialogActions>
        <MuiButton variant="outlined" onClick={onClose}>
          Cancel
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}
