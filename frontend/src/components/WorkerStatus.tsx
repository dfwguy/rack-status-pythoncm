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
import { Flex, Text } from "@nvidia/foundations-react-core";
import type { NativeWorkers } from "../types/api";

interface WorkerStatusProps {
  workers: NativeWorkers;
}

export function WorkerStatusPanel({ workers }: WorkerStatusProps) {
  const availability = workers.availability;
  const sheet = availability?.sheet;
  const sheetMismatch = sheet?.ok && sheet.matches_live === false;
  const sheetDelta = sheet?.delta ?? 0;
  const signedSheetDelta = `${sheetDelta > 0 ? "+" : ""}${sheetDelta.toLocaleString()}`;
  const otherNodes = availability
    ? Math.max(
        0,
        availability.total_nodes
          - availability.ready_nodes
          - availability.unavailable_nodes
          - availability.invalid_nodes,
      )
    : 0;
  return (
    <Flex gap="3" align="center" wrap="wrap" style={{ padding: "8px 4px" }}>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
        Backend: <strong>{workers.mode}</strong>
      </Text>
      {workers.rack_count != null && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {workers.rack_count} racks
        </Text>
      )}
      {availability?.source === "slurm" && (
        <>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Slurm: <strong>{availability.total_nodes.toLocaleString()}</strong> nodes
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--color-green-700, #2e7d32)" }}>
            <strong>{availability.ready_nodes.toLocaleString()}</strong> idle/responding
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            <strong>{otherNodes.toLocaleString()}</strong> allocated/mixed/other (not unavailable)
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--color-red-700, #c62828)" }}>
            <strong>{availability.unavailable_nodes.toLocaleString()}</strong> unavailable = {availability.drained_nodes.toLocaleString()} drained + {availability.down_other_nodes.toLocaleString()} down
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--color-orange-700, #b45309)" }}>
            <strong>{availability.invalid_nodes.toLocaleString()}</strong> invalid
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {availability.no_response_nodes.toLocaleString()} Slurmd no-response flags (overlap; not additive)
          </Text>
          {sheet?.ok && sheet.unavailable != null && (
            <Text
              kind="body/regular/sm"
              title={`Node down status report${sheet.updated_at ? ` · ${sheet.updated_at}` : ""}`}
              style={{ color: sheetMismatch ? "var(--color-orange-700, #b45309)" : "var(--color-green-700, #2e7d32)" }}
            >
              Sheet snapshot: <strong>{sheet.unavailable.toLocaleString()}</strong> unavailable · live now {availability.unavailable_nodes.toLocaleString()} ({signedSheetDelta})
              {sheet.updated_at ? ` · as of ${sheet.updated_at}` : ""}
            </Text>
          )}
          {sheet && !sheet.ok && (
            <Text kind="body/regular/sm" title={sheet.error} style={{ color: "var(--color-orange-700, #b45309)" }}>
              Sheet reconciliation unavailable
            </Text>
          )}
        </>
      )}
      {workers.note && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", fontStyle: "italic" }}>
          {workers.note}
        </Text>
      )}
    </Flex>
  );
}
