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
/**
 * Turn the BCM/VictoriaMetrics SU + rack labels into the operator-facing rack moniker.
 *
 * Metrics must still be queried with the raw rack label (for example `EH1A1`), so this
 * helper is intentionally display-only. The metrics series' `su` label provides the
 * operator-facing scale-unit number: `rack=EH1A1, su=SU1` displays as `SU1A01`.
 */
export function metricsRackMoniker(rawRack: string, rawSu = ""): string {
  const rack = rawRack.trim();
  const location = /^(?:WH|EH)\d+([a-z])(\d+)$/i.exec(rack);
  const short = /^([a-z])(\d+)$/i.exec(rack);
  const coordinates = location ?? short;

  if (!coordinates) return rack;

  const row = coordinates[1].toUpperCase();
  const ordinal = coordinates[2].padStart(2, "0");
  const su = /^SU0*(\d+)$/i.exec(rawSu.trim());

  return su ? `SU${su[1]}${row}${ordinal}` : `${row}${ordinal}`;
}

export interface MetricsRackOption {
  /** Raw VictoriaMetrics rack label; this is the backend query value. */
  rack: string;
  su: string;
  /** Operator-facing label, for example `SU1A01`. */
  label: string;
}

interface MetricsRackLabels {
  rack?: string;
  su?: string;
}

/**
 * Build a stable, unique rack menu from VictoriaMetrics series labels.
 *
 * A populated SU label wins over an older duplicate series that only has `rack`.
 */
export function buildMetricsRackOptions(rows: MetricsRackLabels[]): MetricsRackOption[] {
  const byRack = new Map<string, MetricsRackOption>();

  for (const row of rows) {
    const rack = row.rack?.trim() ?? "";
    const su = row.su?.trim() ?? "";
    if (!rack) continue;

    const previous = byRack.get(rack);
    if (!previous || (!previous.su && su)) {
      byRack.set(rack, { rack, su, label: metricsRackMoniker(rack, su) });
    }
  }

  return [...byRack.values()].sort(compareMetricsRackOptions);
}

/** Natural scale-unit/rack ordering (`SU2A01` before `SU12A01`). */
export function compareMetricsRackOptions(a: MetricsRackOption, b: MetricsRackOption): number {
  return a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: "base" });
}

/** Natural rack ordering retained for callers that only have raw rack labels. */
export function compareMetricsRacks(a: string, b: string): number {
  return metricsRackMoniker(a).localeCompare(metricsRackMoniker(b), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}
