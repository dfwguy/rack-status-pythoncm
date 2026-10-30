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
import { useMemo, useState } from "react";
import { Banner, Button, Checkbox, FormField, SegmentedControl, Skeleton, Tag, Text, TextInput } from "@nvidia/foundations-react-core";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import { RefreshCw, Search } from "lucide-react";
import { RackFloorView } from "../../components/RackFloorView";
import { hasConfiguredFloorLayout } from "../../lib/rackTopology";
import type { RackRow } from "../../types/api";
import type { RackDirectory } from "./types";

interface Props {
  directory: RackDirectory | null;
  loading: boolean;
  error: string;
  selected: string;
  onSelect: (rack: string) => void;
  onRefresh: () => void;
}

export function RackSelector({ directory, loading, error, selected, onSelect, onRefresh }: Props) {
  const [search, setSearch] = useState("");
  const [idleOnly, setIdleOnly] = useState(false);
  const [view, setView] = useState("table");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const racks = useMemo(() => (directory?.racks || []).filter((rack) =>
    rack.name.toLowerCase().includes(search.toLowerCase()) && (!idleOnly || rack.counts.idle > 0)),
    [directory, search, idleOnly]);
  const floorRows: RackRow[] = racks.map((rack) => ({
    rack: rack.name, nemo: "-", last_job: "-", ct_down_unk: "-",
    sinfo: Object.entries(rack.counts).filter(([, count]) => count > 0).map(([state, count]) => state + "=" + count).join(", "),
  }));
  const currentPage = Math.min(page, Math.max(0, Math.ceil(racks.length / pageSize) - 1));
  const chosen = directory?.racks.find((rack) => rack.name === selected);
  const snapshot = directory?.snapshot;
  const physicalLayout = hasConfiguredFloorLayout(directory?.hierarchy, directory?.naming);
  return (
    <section className="ck-section" aria-label="Rack inventory">
      <div className="ck-bar">
        <h2>1. Choose a rack</h2>
        <Tag readOnly color="gray">{directory?.racks.length ?? 0} racks</Tag>
        <Button kind="secondary" color="neutral" size="small" aria-label="Refresh rack inventory" disabled={loading} onClick={onRefresh}>
          <RefreshCw size={16} /> Refresh
        </Button>
      </div>
      <Text kind="body/regular/md" style={{ display: "block", marginTop: 12 }}>
        Choose a rack, then a test size. Availability is checked again before submission.
      </Text>
      {snapshot ? <p className="ck-meta" style={{ marginTop: 8 }}>
        {snapshot.source || "Canonical inventory"} · {snapshot.state}
        {snapshot.age_seconds != null ? ` · ${Math.round(snapshot.age_seconds)}s old` : ""}
        {snapshot.last_success_at ? ` · updated ${new Date(snapshot.last_success_at).toLocaleString()}` : ""}
      </p> : null}
      {error || directory?.error ? <Banner status="error" style={{ marginTop: 12 }}>{error || directory?.error}</Banner> : null}
      {snapshot && !["fresh", "configured"].includes(snapshot.state) ? (
        <Banner status="warning" style={{ marginTop: 12 }}>Inventory is stale or unavailable. You can inspect it; new test previews require fresh authoritative placement.</Banner>
      ) : null}
      <div className="ck-fields">
        <FormField slotLabel="Search racks" name="ck-rack-search">
          <TextInput aria-label="Search racks" value={search} slotStart={<Search size={16} />}
            onChange={(event) => { setSearch(event.target.value); setPage(0); }} />
        </FormField>
        <Checkbox slotLabel="With idle nodes" checked={idleOnly} onCheckedChange={(value) => { setIdleOnly(value === true); setPage(0); }} />
        <SegmentedControl aria-label="Rack inventory layout" value={view}
          items={[{ value: "table", children: "Table" }, { value: "floor", children: physicalLayout ? "Floor" : "Tiles" }]} onValueChange={setView} />
        <span className="ck-meta">{racks.length} matching racks</span>
      </div>
      {loading && !directory ? <Skeleton style={{ height: 260 }} /> : view === "floor" && directory ? (
        <div className="ck-rack-floor">
          {!physicalLayout ? <p className="ck-meta" style={{ marginBottom: 12 }}>
            Rack overview; physical floor positions are not configured.
          </p> : null}
          <RackFloorView unconfiguredLayout={physicalLayout ? undefined : "tiles"} rows={floorRows} nodes={directory.nodes} nodeSources={directory.node_sources}
            hierarchy={directory.hierarchy || undefined} naming={directory.naming || undefined}
            scalableUnits={directory.scalable_units} selectedRack={selected} initialStatusMode="scheduler"
            onRackSelect={onSelect} />
        </div>
      ) : (
        <>
          <TableContainer sx={{ maxHeight: 380, mt: 1 }}>
            <Table size="small" stickyHeader aria-label="Rack availability">
              <TableHead><TableRow>
                {["Rack", "Mapped nodes", "Idle", "Busy", "Drained", "Down", "Reserved", "Unknown", "Testing"].map((label) => <TableCell key={label}>{label}</TableCell>)}
              </TableRow></TableHead>
              <TableBody>
                {racks.slice(currentPage * pageSize, (currentPage + 1) * pageSize).map((rack) => (
                  <TableRow key={rack.name} hover selected={selected === rack.name}>
                    <TableCell><Button kind={selected === rack.name ? "primary" : "secondary"}
                      color={selected === rack.name ? "brand" : "neutral"} size="small"
                      aria-label={`Select rack ${rack.name}`} aria-pressed={selected === rack.name}
                      onClick={() => onSelect(rack.name)}>{rack.name}</Button></TableCell>
                    <TableCell>{rack.node_count}</TableCell>
                    <TableCell>{rack.counts.idle}</TableCell><TableCell>{rack.counts.busy}</TableCell>
                    <TableCell>{rack.counts.drain}</TableCell><TableCell>{rack.counts.down}</TableCell>
                    <TableCell>{rack.counts.reserved}</TableCell><TableCell>{rack.counts.unknown}</TableCell>
                    <TableCell><Tag readOnly color={rack.test_enabled ? "green" : "gray"}>
                      {rack.test_enabled ? "Enabled" : rack.authoritative ? "Not enabled" : "No placement"}
                    </Tag></TableCell>
                  </TableRow>
                ))}
                {!racks.length && !loading ? <TableRow><TableCell colSpan={9}>No matching racks</TableCell></TableRow> : null}
              </TableBody>
            </Table>
          </TableContainer>
          <TablePagination component="div" count={racks.length} rowsPerPage={pageSize} page={currentPage}
            rowsPerPageOptions={[10, 25, 50]} onPageChange={(_, next) => setPage(next)}
            onRowsPerPageChange={(event) => { setPageSize(Number(event.target.value)); setPage(0); }} />
        </>
      )}
      {chosen ? <div className="ck-bar" style={{ marginTop: 12 }}>
        <Text kind="body/bold/md">Selected: {chosen.name}</Text>
        <span>{chosen.node_count} mapped nodes · {chosen.allowed_node_count} permitted for testing</span>
        {!chosen.test_enabled && chosen.reason ? <Banner status="warning">{chosen.reason}</Banner> : null}
      </div> : <p className="ck-meta" style={{ marginTop: 12 }}>Select a rack to choose a test size and inspect its current node availability.</p>}
      {directory?.unplaced_node_count ? <p className="ck-meta" style={{ marginTop: 8 }}>
        {directory.unplaced_node_count} inventory nodes have no physical rack placement and are not assigned to a rack here.
      </p> : null}
    </section>
  );
}
