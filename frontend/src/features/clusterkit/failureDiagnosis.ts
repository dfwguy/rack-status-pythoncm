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
import { functionalStatus, skippedStages } from "./model";
import type { RunStatus, StageFailure } from "./types";

export interface FailureDiagnosis {
  kind: "communication" | "preflight" | "process" | "evidence" | "scheduler";
  title: string;
  stage: string;
  exitStatus?: number | null;
  nodes: string[];
  nodeLabel: string;
  nodeFinding: string;
  explanation: string;
  preflight: string;
  coverage?: string;
  missingMeasurements: boolean;
  evidence: NonNullable<StageFailure["evidence"]>;
  nextStep: string;
}

export function stageLabel(name: string): string {
  return name === "baseline" ? "Baseline" : name === "single-path" ? "Single-path" : name;
}

/** Describe this attempt only. A UCX reporter is not a diagnosed hardware fault. */
export function failureDiagnosis(run: RunStatus): FailureDiagnosis | null {
  if (run.work_phase === "submission_unknown") return null;
  if (run.summary_error) return {
    kind: "evidence", title: "Run evidence could not be verified", stage: "Result verification",
    nodes: [], nodeLabel: "Reported by", nodeFinding: "", explanation: run.summary_error,
    preflight: "Not verified", evidence: [], missingMeasurements: false,
    nextStep: "Review the saved artifacts and verification error before interpreting node or pair results.",
  };
  if (functionalStatus(run) !== "FAIL") return null;
  const summary = run.summary;
  // Only associate a finding with exact nodes belonging to this verified attempt.
  const allowed = new Set(run.activity.nodes.filter((node) => !summary || summary.nodes.includes(node)));
  const preflightNodes = (summary?.preflight?.nodes || []).filter((node) => allowed.has(node.host));
  const passed = new Set(preflightNodes.filter((node) => node.status === "PASS").map((node) => node.host)).size;
  const preflight = summary?.preflight
    ? `${summary.preflight.status} · ${passed}/${allowed.size} nodes passed local checks`
    : "No local preflight evidence";
  const failedPreflight = preflightNodes.filter((node) => node.status === "FAIL");
  if (summary?.preflight?.status === "FAIL") return {
    kind: "preflight", title: "Node preflight failed", stage: "Preflight",
    exitStatus: summary.preflight.exit_status, nodes: [...new Set(failedPreflight.map((node) => node.host))],
    nodeLabel: "Failed local check", nodeFinding: "Local check failed",
    explanation: "A required local readiness check failed before pairwise validation could complete.",
    preflight, evidence: [], missingMeasurements: false,
    nextStep: "Open node diagnostics for the failed check and review the preflight artifacts before rerunning.",
  };
  const skipped = skippedStages(summary);
  const stages = Object.entries(summary?.stages || {}).filter(([name]) => !(name in skipped));
  // Prefer the primary baseline failure even when a diagnostic/comparison stage exists.
  stages.sort(([a], [b]) => (a === "baseline" ? -1 : b === "baseline" ? 1 : 0));
  const failed = stages.find(([, stage]) => stage.status === "FAIL"
    || (stage.exit_status != null && stage.exit_status !== 0));
  if (failed) {
    const [name, stage] = failed;
    const communication = stage.failure?.category === "UCX_COMMUNICATION_TIMEOUT";
    const nodes = [...new Set((stage.failure?.affected_nodes || []).filter((node) => allowed.has(node)))];
    const metrics = Object.entries(stage.metrics || {});
    const expected = summary?.expected_pair_count;
    const coverage = metrics.filter(([, data]) => Number.isInteger(data.observed_pair_count)
      && data.observed_pair_count >= 0).map(([metric, data]) =>
      `${metric} ${data.observed_pair_count}/${expected ?? "?"} pairs`).join("; ");
    return {
      kind: communication ? "communication" : "process",
      title: communication ? "Network communication timed out" : "ClusterKit stage failed",
      stage: stageLabel(name), exitStatus: stage.exit_status, nodes, nodeLabel: "Reported by",
      nodeFinding: communication ? "Communication timeout reported" : "Process error reported",
      explanation: communication
        ? "The reporting node logged a UCX/MPI timeout. The peer and fault location are not established by this error."
        : "This stage did not complete successfully. A reporting node alone does not establish the cause.",
      preflight, coverage: coverage || (expected ? "No pair measurements collected" : undefined),
      missingMeasurements: metrics.some(([, data]) => Boolean(data.missing_pairs?.length)),
      evidence: (stage.failure?.evidence || []).slice(0, 4).map((entry) => ({
        artifact: entry.artifact, line: Number.isInteger(entry.line) && entry.line! > 0 ? entry.line : undefined,
        excerpt: entry.excerpt.slice(0, 1200),
      })),
      nextStep: communication
        ? "Share this run's job ID, reporting node and log excerpt with the fabric/node team. Local preflight PASS does not verify inter-node communication."
        : "Review the stage log and node diagnostics for the first error before rerunning.",
    };
  }
  return {
    kind: "scheduler", title: "Run did not complete successfully", stage: "Run / scheduler",
    nodes: [], nodeLabel: "Reported by", nodeFinding: "", preflight,
    explanation: "No verified node-specific cause is available for this attempt. See Run notes and saved artifacts.",
    evidence: [], missingMeasurements: false,
    nextStep: "Check the scheduler outcome and saved logs; do not infer a faulty node from missing results.",
  };
}
