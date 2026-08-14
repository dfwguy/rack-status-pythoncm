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
 * Turn the BCM/VictoriaMetrics rack label into the operator-facing rack moniker.
 *
 * Metrics must still be queried with the raw label (for example `WH1G1`), so this
 * helper is intentionally display-only.  Already-short labels are canonicalised
 * as well because older collectors have emitted both `WH1H2` and `h02`.
 */
export function metricsRackMoniker(raw: string): string {
  const value = raw.trim();
  const bcm = /^WH\d+([a-z])(\d+)$/i.exec(value);
  if (bcm) return `${bcm[1].toLowerCase()}${bcm[2].padStart(2, "0")}`;

  const short = /^([a-z])(\d+)$/i.exec(value);
  if (short) return `${short[1].toLowerCase()}${short[2].padStart(2, "0")}`;

  return value;
}

/** Natural rack ordering (`g02` before `g10`) based on the displayed moniker. */
export function compareMetricsRacks(a: string, b: string): number {
  return metricsRackMoniker(a).localeCompare(metricsRackMoniker(b), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}
