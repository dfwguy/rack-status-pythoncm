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
import { useState } from "react";
import IconButton from "@mui/material/IconButton";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import { Button, Flex, Text } from "@nvidia/foundations-react-core";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { DeploymentState, TrackerRollup, TrackerUnit } from "../types";
import { STATE_COLOR, STATE_LABEL, formatCount, formatPercent, plural } from "../lib/trackerModel";

const COLUMNS: DeploymentState[] = ["not_started", "in_progress", "remediation", "completed", "handed_over"];

interface LineProps {
  depth: number;
  name: string;
  detail?: string;
  rollup: TrackerRollup;
  columns: DeploymentState[];
  expandable: boolean;
  open: boolean;
  onToggle: () => void;
}

function Line({ depth, name, detail, rollup, columns, expandable, open, onToggle }: LineProps) {
  const pct = rollup.percent_complete;
  return (
    <TableRow hover sx={depth === 0 ? { bgcolor: "action.hover" } : undefined}>
      <TableCell sx={{ pl: 1 + depth * 3 }}>
        <Flex align="center" gap="1">
          {expandable ? (
            <IconButton size="small" aria-label={`${open ? "Collapse" : "Expand"} ${name}`} aria-expanded={open} onClick={onToggle}>
              {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </IconButton>
          ) : (
            <span style={{ display: "inline-block", width: 34 }} />
          )}
          <Text kind={depth === 0 ? "body/bold/md" : "body/regular/md"}>{name}</Text>
          {detail ? (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {detail}
            </Text>
          ) : null}
        </Flex>
      </TableCell>
      <TableCell align="right">
        <Text kind={depth === 0 ? "body/bold/md" : "body/regular/md"}>{formatCount(rollup.total)}</Text>
      </TableCell>
      {columns.map((state) => {
        const n = rollup.counts[state];
        return (
          <TableCell key={state} align="right">
            <Text
              kind={depth === 0 ? "body/bold/md" : "body/regular/md"}
              style={{ color: n === 0 ? "var(--text-color-subtle)" : undefined }}
            >
              {formatCount(n)}
            </Text>
          </TableCell>
        );
      })}
      <TableCell>
        <Flex align="center" gap="2">
          <div
            aria-hidden
            style={{ width: 72, height: 6, borderRadius: 3, overflow: "hidden", background: "var(--background-color-component-track)" }}
          >
            <div style={{ width: `${Math.min(100, pct)}%`, height: "100%", background: STATE_COLOR.completed }} />
          </div>
          <Text kind="body/bold/sm">{formatPercent(pct)}</Text>
        </Flex>
      </TableCell>
    </TableRow>
  );
}

/** SU -> row -> rack, with the counts at every level; click a line to open the level below. */
export function TrackerTable({ units }: { units: TrackerUnit[] }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set(units.slice(0, 1).map((u) => u.id)));
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const columns = COLUMNS.filter((s) => s !== "handed_over" || units.some((u) => u.counts.handed_over > 0));
  const allKeys = units.flatMap((u) => [u.id, ...u.rows.map((r) => `${u.id}|${r.key}`)]);

  return (
    <Flex direction="col" gap="2">
      <Flex gap="2">
        <Button kind="secondary" color="neutral" size="small" onClick={() => setOpen(new Set(units.map((u) => u.id)))}>
          Expand scalable units
        </Button>
        <Button kind="secondary" color="neutral" size="small" onClick={() => setOpen(new Set(allKeys))}>
          Expand to racks
        </Button>
        <Button kind="secondary" color="neutral" size="small" onClick={() => setOpen(new Set())}>
          Collapse all
        </Button>
      </Flex>
      <TableContainer>
        <Table size="small" aria-label="Node detail by scalable unit, row and rack">
          <TableHead>
            <TableRow>
              <TableCell>Scalable unit / row / rack</TableCell>
              <TableCell align="right">Total nodes</TableCell>
              {columns.map((state) => (
                <TableCell key={state} align="right">
                  {STATE_LABEL[state]}
                </TableCell>
              ))}
              <TableCell>% complete</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {units.map((unit) => {
              const unitOpen = open.has(unit.id);
              return [
                <Line
                  key={unit.id}
                  depth={0}
                  name={unit.label}
                  detail={unit.handed_over_at ? `handed over ${unit.handed_over_at}` : `${plural(unit.rows.length, "row")} · ${plural(unit.rack_count, "rack")}`}
                  rollup={unit}
                  columns={columns}
                  expandable
                  open={unitOpen}
                  onToggle={() => toggle(unit.id)}
                />,
                ...(unitOpen
                  ? unit.rows.flatMap((row) => {
                      const key = `${unit.id}|${row.key}`;
                      const rowOpen = open.has(key);
                      return [
                        <Line
                          key={key}
                          depth={1}
                          name={row.label}
                          detail={plural(row.racks.length, "rack")}
                          rollup={row}
                          columns={columns}
                          expandable
                          open={rowOpen}
                          onToggle={() => toggle(key)}
                        />,
                        ...(rowOpen
                          ? row.racks.map((rack) => (
                              <Line
                                key={`${key}|${rack.rack}`}
                                depth={2}
                                name={rack.label}
                                detail={`rack stages ${rack.stage1} / ${rack.stage2}`}
                                rollup={rack}
                                columns={columns}
                                expandable={false}
                                open={false}
                                onToggle={() => undefined}
                              />
                            ))
                          : []),
                      ];
                    })
                  : []),
              ];
            })}
          </TableBody>
        </Table>
      </TableContainer>
    </Flex>
  );
}
