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
import type { InventoryNode, RunStatus, Readiness, Summary } from "./types";

export function nodeAvailability(
  node: InventoryNode,
  partition?: string,
): "idle" | "busy" | "unavailable" {
  if (
    node.allowed === false ||
    !partition ||
    !node.partitions.includes(partition) ||
    !node.states.length ||
    node.states.some((state) => !["IDLE", "ALLOCATED", "MIXED"].includes(state))
  )
    return "unavailable";
  return node.states.every((state) => state === "IDLE") ? "idle" : "busy";
}
export function terminal(run: RunStatus): boolean {
  return (
    ["done", "error", "timeout"].includes(run.phase) &&
    run.work_phase !== "submission_unknown"
  );
}
export function functionalStatus(run: RunStatus): string {
  if (run.summary_error) return "INVALID EVIDENCE";
  if (["error", "timeout"].includes(run.phase)) return "FAIL";
  if (run.summary?.functional_status === "FAIL") return "FAIL";
  if (
    run.phase === "done" &&
    run.scheduler_state === "COMPLETED" &&
    run.summary?.functional_status
  )
    return run.summary.functional_status;
  return terminal(run) ? "FAIL" : "PENDING";
}
export function dateTime(value?: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function rackSelectionEnabled(readiness: Readiness): boolean {
  return Boolean(readiness.rack_inventory_enabled || (readiness.whole_rack_enabled
    && readiness.groups?.some((group) => group.kind === "rack" && group.authoritative)));
}

/** Old summaries may list absent artifacts for a stage the runner deliberately skipped. */
export function skippedStages(summary: Summary | null | undefined): Record<string, string> {
  const skipped: Record<string, string> = {};
  for (const [name, stage] of Object.entries(summary?.stages || {})) {
    if (stage.exit_status != null) continue;
    if (stage.status === "SKIPPED") {
      skipped[name] = stage.reason || "Not run after an earlier failure";
    } else if (summary?.clusterkit_exit_status && !(name in summary.clusterkit_exit_status)) {
      if (summary.preflight?.status === "FAIL") {
        skipped[name] = "Not run because preflight did not complete successfully";
      } else if (name === "single-path" && summary.clusterkit_exit_status.baseline != null
        && summary.clusterkit_exit_status.baseline !== 0) {
        skipped[name] = "Not run because baseline failed";
      }
    }
  }
  return skipped;
}

export function summaryIssues(summary: Summary | null | undefined): string[] {
  const skipped = skippedStages(summary);
  return (summary?.errors || []).filter((message) => !Object.keys(skipped).some((stage) =>
    message === stage + ": process did not complete successfully"
    || message === stage + ": Missing bandwidth JSON"
    || message === stage + ": Missing latency JSON"
    || (message.startsWith(stage + " rank ") && message.includes("No such file or directory"))));
}
