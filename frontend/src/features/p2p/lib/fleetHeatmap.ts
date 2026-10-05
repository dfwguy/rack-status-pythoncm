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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Heatmap encoding for catalog coverage: pure functions and the palette, with
// no React in the file so the rules can be unit-tested without rendering.
//
// TWO-CHANNEL ENCODING. The fill answers "how far through the catalog is this
// node"; the border answers "is what it already holds trustworthy". They are
// separate channels because a single state label hides the case that actually
// matters operationally -- a node can read `complete` on count while holding a
// wrong-sized or never-hashed file, and folding that into one colour makes the
// bad node indistinguishable from a good one. This is the same reason the Rack
// Status node heatmap puts liveness on the border and the selected metric on
// the fill: one picture, two questions, neither hidden by the other.
//
// The hex values here are a deliberate exception to the design-token rule. A
// data-encoding scale has no token equivalent, and centralising it in one
// module is what stops call sites inventing their own greens. Chrome (text,
// surfaces, borders of real components) still uses tokens.
import type { NodeCoverage } from "../types/fleet";

/** How far through the required catalog a node is. Drives the cell fill. */
export type CoverageTone = "complete" | "syncing" | "incomplete" | "unavailable";

/** Whether what the node already holds can be trusted. Drives the cell border. */
export type IntegrityTone = "verified" | "unverified" | "corrupt" | "unknown";

/** Matches the Rack Status heatmap palette so the two consoles read alike. */
export const COVERAGE_FILL: Record<CoverageTone, string> = {
  complete: "#16a34a",
  syncing: "#2563eb",
  incomplete: "#eab308",
  unavailable: "#6b7280",
};

export const INTEGRITY_BORDER: Record<IntegrityTone, string> = {
  // A darker shade of the fill: present, but deliberately not competing with it.
  verified: "#15803d",
  // Amber: the node has content nobody has hashed yet.
  unverified: "#b45309",
  // Red, and the only border that overrides a green fill. A wrong-sized file is
  // the one condition an operator must never have to hover to discover.
  corrupt: "#dc2626",
  unknown: "#4b5563",
};

export const COVERAGE_LABEL: Record<CoverageTone, string> = {
  complete: "Complete",
  syncing: "Syncing",
  incomplete: "Incomplete",
  unavailable: "Unavailable",
};

/** Node states that mean "not participating", per the server's vocabulary. */
const OUT_OF_PLAY: ReadonlySet<string> = new Set(["not-eligible", "unreachable", "unavailable"]);

/**
 * Fill tone for a node.
 *
 * `complete` requires the verified count to reach the catalog size -- not just
 * the present count. A full-size file is not proof of completion, so a node
 * with every file present but nothing hashed is `incomplete` here, exactly as
 * the deployment guide requires.
 */
export function coverageToneOf(node: NodeCoverage, requiredCount: number): CoverageTone {
  if (OUT_OF_PLAY.has(node.state)) return "unavailable";
  if (node.active > 0 || node.state === "syncing") return "syncing";
  if (node.state === "complete" && node.verified === requiredCount) return "complete";
  return "incomplete";
}

/**
 * Border tone for a node.
 *
 * Corruption outranks everything: it is reported even on a node that is
 * otherwise complete, because that is the combination a count-based view loses.
 */
export function integrityToneOf(node: NodeCoverage): IntegrityTone {
  if (node.bad_size > 0) return "corrupt";
  if (node.unverified > 0) return "unverified";
  if (OUT_OF_PLAY.has(node.state)) return "unknown";
  return "verified";
}

export type ToneCounts = Record<CoverageTone, number>;

export function toneCounts(nodes: NodeCoverage[], requiredCount: number): ToneCounts {
  const counts: ToneCounts = { complete: 0, syncing: 0, incomplete: 0, unavailable: 0 };
  for (const node of nodes) counts[coverageToneOf(node, requiredCount)] += 1;
  return counts;
}

/** Nodes needing an operator's attention: corrupt content, or unhashed content. */
export function integrityExceptions(nodes: NodeCoverage[]): NodeCoverage[] {
  return nodes.filter((node) => {
    const tone = integrityToneOf(node);
    return tone === "corrupt" || tone === "unverified";
  });
}

/** Group nodes by SU, each list sorted by node name for a stable grid. */
export function groupBySu(nodes: NodeCoverage[]): Map<number, NodeCoverage[]> {
  const grouped = new Map<number, NodeCoverage[]>();
  for (const node of nodes) {
    const bucket = grouped.get(node.su);
    if (bucket) bucket.push(node);
    else grouped.set(node.su, [node]);
  }
  for (const bucket of grouped.values()) {
    bucket.sort((a, b) => a.node.localeCompare(b.node));
  }
  return grouped;
}

/**
 * Tooltip text for one cell.
 *
 * Built here rather than in the component so the exact same string can be used
 * as the accessible label, and so it is testable. Newline-separated; the
 * renderer decides how to present the lines.
 */
export function cellTitle(node: NodeCoverage, requiredCount: number): string {
  const lines: string[] = [
    node.node,
    `${COVERAGE_LABEL[coverageToneOf(node, requiredCount)]} \u00b7 ${node.verified}/${requiredCount} verified \u00b7 ${node.present} present`,
  ];

  if (node.bad_size > 0) lines.push(`${node.bad_size} file(s) with a wrong size`);
  if (node.unverified > 0) lines.push(`${node.unverified} file(s) present but never hashed`);

  const byState = (state: string) =>
    Object.entries(node.images)
      .filter(([, value]) => value === state)
      .map(([filename]) => filename);

  const missing = [...byState("missing"), ...byState("bad-size")];
  if (missing.length) lines.push(`Missing or bad: ${missing.join(", ")}`);

  if (node.sync_state) {
    lines.push(`Wave: ${node.sync_state}${node.sync_detail ? ` \u2014 ${node.sync_detail}` : ""}`);
  }

  if (!Object.keys(node.images).length && node.missing > 0) {
    lines.push("No per-image detail in this snapshot; run Verify fleet.");
  }

  lines.push(node.observed_at ? `Verified: ${node.observed_at}` : "No verification observation");
  return lines.join("\n");
}
