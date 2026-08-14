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
/** Matplotlib "YlGn"–like stops (light → dark). */
export const YL_GN_STOPS: Array<{ t: number; r: number; g: number; b: number }> = [
  { t: 0, r: 255, g: 255, b: 229 },
  { t: 0.25, r: 247, g: 252, b: 185 },
  { t: 0.5, r: 173, g: 221, b: 142 },
  { t: 0.75, r: 120, g: 198, b: 121 },
  { t: 1, r: 0, g: 104, b: 55 },
];

/** sRGB channel 0–255 → linear 0–1 (WCAG). */
function srgbToLinear(c255: number): number {
  const x = c255 / 255;
  return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
}

/** Relative luminance 0–1 for sRGB 0–255. */
export function relativeLuminanceRgb(r255: number, g255: number, b255: number): number {
  const r = srgbToLinear(r255);
  const g = srgbToLinear(g255);
  const b = srgbToLinear(b255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Interpolated YlGn RGB for ``norm01`` in [0, 1] (same mapping as ``ylGnColor``). */
export function ylGnRgb(norm01: number): [number, number, number] {
  const t = Math.max(0, Math.min(1, norm01));
  let lo = YL_GN_STOPS[0];
  let hi = YL_GN_STOPS[YL_GN_STOPS.length - 1];
  for (let s = 0; s < YL_GN_STOPS.length - 1; s++) {
    if (t >= YL_GN_STOPS[s].t && t <= YL_GN_STOPS[s + 1].t) {
      lo = YL_GN_STOPS[s];
      hi = YL_GN_STOPS[s + 1];
      break;
    }
  }
  const span = hi.t - lo.t || 1;
  const u = (t - lo.t) / span;
  const r = Math.round(lo.r + (hi.r - lo.r) * u);
  const g = Math.round(lo.g + (hi.g - lo.g) * u);
  const b = Math.round(lo.b + (hi.b - lo.b) * u);
  return [r, g, b];
}

export function ylGnColor(norm01: number): string {
  const [r, g, b] = ylGnRgb(norm01);
  return `rgb(${r},${g},${b})`;
}

/**
 * Text color that contrasts with the YlGn cell at the same ``norm01`` used for the background.
 * Prefer this over value/threshold heuristics when the colormap is not monotonic in the metric (e.g. latency “goodness”).
 */
export function textColorForYlGnNorm(norm01: number): string {
  const [r, g, b] = ylGnRgb(norm01);
  const L = relativeLuminanceRgb(r, g, b);
  return L > 0.52 ? "#111111" : "#f8fafc";
}

export function cellTextColorForHeatmap(value: number, threshold: number): string {
  return value > threshold ? "#ffffff" : "#111111";
}
