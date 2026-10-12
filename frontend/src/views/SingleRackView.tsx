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
import { useState, useCallback } from "react";
import Box from "@mui/material/Box";
import { Flex, SegmentedControl, Skeleton, Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { StatusSummary, type ValidationSummaryCategory } from "../components/StatusSummary";
import { RackTable } from "../components/RackTable";
import { RackFloorView } from "../components/RackFloorView";
import { NodePoolView } from "../components/NodePoolView";
import { WorkerStatusPanel } from "../components/WorkerStatus";
import { FooterNotes } from "../components/FooterNotes";
import { LoadingSkeleton } from "../components/LoadingSkeleton";
import { ErrorBanner } from "../components/ErrorBanner";
import { ClusterSnapshotState } from "../components/ClusterSnapshotState";
import type { RackStatusResponse } from "../types/api";

type DashboardView = "table" | "floor";
const VIEW_STORAGE_KEY = "nvis.singlerack.view";
const VIEW_ITEMS = [
  { value: "table", children: "Table" },
  { value: "floor", children: "Floor" },
];

interface SingleRackViewProps {
  data: RackStatusResponse | null;
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
  onRackSelect?: (rack: string) => void;
  onCommentChange?: (rack: string, comment: string | null) => void;
}

/** Per-rack validation table, workers, and filters (formerly the main dashboard). */
export function SingleRackView({ data, loading, error, onRetry, onRackSelect, onCommentChange }: SingleRackViewProps) {
  const [validationFilters, setValidationFilters] = useState<string[]>([]);
  const [view, setView] = useState<DashboardView>(
    () => ((typeof localStorage !== "undefined" && localStorage.getItem(VIEW_STORAGE_KEY)) as DashboardView) || "table"
  );

  const onViewChange = useCallback((v: string) => {
    const next = (v === "floor" ? "floor" : "table") as DashboardView;
    setView(next);
    try {
      localStorage.setItem(VIEW_STORAGE_KEY, next);
    } catch {
      /* ignore persistence errors */
    }
  }, []);

  const toggleValidationCategory = useCallback((category: ValidationSummaryCategory) => {
    setValidationFilters((prev) =>
      prev.includes(category) ? prev.filter((c) => c !== category) : [...prev, category]
    );
  }, []);

  if (loading && !data) return <LoadingSkeleton />;

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader contentId="singleRack" title="Rack Validation Status" description="Overview of single-rack NEMO/NCCL validation progress." />

      {error && <ErrorBanner message={error} />}

      {data?.snapshot && (
        <Box sx={{ mb: 2 }}>
          <ClusterSnapshotState snapshot={data.snapshot} onRetry={onRetry} />
        </Box>
      )}

      {data && data.rack_mapping_mode === "pool" ? (
        // No placement data: render the flat node pool rather than empty rack rows. The rack
        // table/floor assume racks exist; pool clusters have none, so the node is the atom.
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <NodePoolView nodes={data.nodes} inventoryError={data.node_inventory_error} />
          <FooterNotes footnotes={data.footnotes} />
        </Box>
      ) : data && (data.snapshot.state === "fresh" || data.snapshot.state === "stale") ? (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <StatusSummary
            summary={data.summary}
            updatedAt={data.updated_at}
            legend={data.legend}
            validationFilters={validationFilters}
            onToggleValidationCategory={toggleValidationCategory}
          />

          {data.native_workers && (
            <WorkerStatusPanel workers={data.native_workers} />
          )}

          <Flex justify="end">
            <SegmentedControl
              size="small"
              items={VIEW_ITEMS}
              value={view}
              onValueChange={onViewChange}
              aria-label="Dashboard layout"
            />
          </Flex>

          {view === "floor" ? (
            <RackFloorView
              rows={data.rows}
              naming={data.naming}
              hierarchy={data.hierarchy}
              scalableUnits={data.scalable_units}
              nodes={data.nodes}
              nodeSources={data.node_sources}
              onRackSelect={onRackSelect}
            />
          ) : (
            <RackTable
              rows={data.rows}
              comments={data.comments}
              logDir={data.log_dir}
              naming={data.naming}
              onRackSelect={onRackSelect}
              onCommentChange={onCommentChange}
              validationFilters={validationFilters}
              onValidationFiltersChange={setValidationFilters}
            />
          )}

          <FooterNotes footnotes={data.footnotes} />
        </Box>
      ) : data?.snapshot.state === "initializing" ? (
        <Box
          aria-label="Loading rack inventory"
          role="status"
          sx={{ display: "flex", flexDirection: "column", gap: 1.5, mt: 1 }}
        >
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Rack table will appear when inventory is ready.
          </Text>
          <Skeleton style={{ height: 48 }} />
          <Skeleton style={{ height: 120 }} />
        </Box>
      ) : data ? (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1, mt: 1 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Rack table unavailable until inventory collection succeeds.
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}
