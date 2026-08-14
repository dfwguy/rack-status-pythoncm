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
/** Small helpers for editing catalog docs in textual form fields. */

export const listToCsv = (xs: string[] | undefined): string => (xs || []).join(", ");

export const csvToList = (s: string): string[] =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

/** Render a {k: v} map as newline-separated `KEY=value` lines. */
export const mapToText = (m: Record<string, string> | undefined): string =>
  Object.entries(m || {})
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");

/** Parse newline-separated `KEY=value` lines back into a map. */
export const textToMap = (s: string): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const line of s.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.includes("=")) continue;
    const idx = trimmed.indexOf("=");
    const key = trimmed.slice(0, idx).trim();
    const val = trimmed.slice(idx + 1).trim();
    if (key) out[key] = val;
  }
  return out;
};

export const intOr = (s: string, fallback: number): number => {
  const n = parseInt(s, 10);
  return Number.isFinite(n) ? n : fallback;
};
