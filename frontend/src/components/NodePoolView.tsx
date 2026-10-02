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
import { useMemo } from "react";
import { Banner, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { NodeInventoryEntry } from "../types/api";

/**
 * Flat-pool rendering for a cluster whose `rack_mapping_mode` is "pool": there is no placement
 * data anywhere (no rack objects, no provider rack attribute, no rack token in the hostname), so
 * the node is the atom. Nodes render as a wrapped grid of tiles ordered by hostname — logical
 * order, NOT physical placement. Node *shape* (GPUs per node) still shows, from the scheduler.
 *
 * This deliberately does not draw a rack: a single "rack" card holding the whole fleet would
 * assert a physical fact that does not exist. The honest statement is "no placement data".
 */

const STATUS_TONE: Record<NodeInventoryEntry["status"], "green" | "red" | "yellow" | "gray"> = {
  up: "green",
  down: "red",
  inst: "yellow",
  unknown: "gray",
};

function statusLabel(status: NodeInventoryEntry["status"]): string {
  return status === "unknown" ? "UNKNOWN" : status.toUpperCase();
}

interface NodePoolViewProps {
  nodes: NodeInventoryEntry[] | null | undefined;
  /** Surfaced verbatim when the inventory probe failed or is serving a stale roster. */
  inventoryError?: string;
}

export function NodePoolView({ nodes, inventoryError }: NodePoolViewProps) {
  const sorted = useMemo(
    () =>
      [...(nodes ?? [])].sort((a, b) =>
        a.id.localeCompare(b.id, undefined, { numeric: true })
      ),
    [nodes]
  );

  const totals = useMemo(() => {
    let up = 0;
    let gpus = 0;
    for (const n of sorted) {
      if (n.status === "up") up += 1;
      if (typeof n.gpu_count === "number") gpus += n.gpu_count;
    }
    return { up, gpus };
  }, [sorted]);

  if (nodes === null) {
    return <Banner status="warning">Node inventory is unavailable.</Banner>;
  }

  return (
    <div>
      <Banner status="info" style={{ marginBottom: 16 }}>
        This cluster has no rack placement data (no racks configured on the cluster manager, and the
        hostname does not encode one). Showing a flat node pool ordered by hostname — logical order,
        not physical placement.
      </Banner>

      {inventoryError && (
        <Banner status="warning" style={{ marginBottom: 16 }}>
          {inventoryError}
        </Banner>
      )}

      <Flex gap="2" align="center" style={{ marginBottom: 12 }}>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {sorted.length} nodes · {totals.up} up
          {totals.gpus > 0 ? ` · ${totals.gpus} GPUs` : ""}
        </Text>
      </Flex>

      {sorted.length === 0 ? (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          No nodes reported yet.
        </Text>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))",
            gap: 8,
          }}
        >
          {sorted.map((n) => (
            <div
              key={n.id}
              style={{
                border: "1px solid var(--border-color-subtle, rgba(255,255,255,0.12))",
                borderRadius: 6,
                padding: "8px 10px",
                display: "flex",
                flexDirection: "column",
                gap: 6,
              }}
            >
              <Text kind="mono/sm" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                {n.id}
              </Text>
              <Flex justify="between" align="center" gap="2">
                <Tag color={STATUS_TONE[n.status]} kind="solid" readOnly density="compact">
                  {statusLabel(n.status)}
                </Tag>
                <Text
                  kind="body/regular/sm"
                  style={{ color: n.gpu_count ? undefined : "var(--text-color-secondary)" }}
                  title={n.gpu_type || undefined}
                >
                  {n.gpu_count ? `${n.gpu_count} GPU` : "—"}
                </Text>
              </Flex>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
