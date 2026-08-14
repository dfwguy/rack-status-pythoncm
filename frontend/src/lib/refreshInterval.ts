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
 * Shared auto-refresh interval choices for polled panels.
 *
 * Kept out of the component file on purpose: a module that exports both a
 * component and constants trips `react-refresh/only-export-components`.
 */

/** Sentinel for "do not auto-refresh". */
export const REFRESH_OFF = 0;

/** Default cadence: slow enough not to hammer expensive backends. */
export const DEFAULT_REFRESH_MS = 5 * 60_000;

/** Offered intervals, in milliseconds, in menu order. */
export const REFRESH_INTERVALS_MS = [REFRESH_OFF, 60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000] as const;

/** Menu label for an interval ("Off", "1m", "5m", …). */
export function refreshIntervalLabel(ms: number): string {
  return ms === REFRESH_OFF ? "Off" : `${Math.round(ms / 60_000)}m`;
}

/** Clamp an arbitrary number to a supported interval, else the default. */
export function normalizeRefreshMs(ms: number | null | undefined): number {
  return REFRESH_INTERVALS_MS.includes(ms as (typeof REFRESH_INTERVALS_MS)[number])
    ? (ms as number)
    : DEFAULT_REFRESH_MS;
}

/** Read a persisted interval (localStorage), falling back to the default. */
export function loadRefreshMs(storageKey: string): number {
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw === null) return DEFAULT_REFRESH_MS;
    return normalizeRefreshMs(Number.parseInt(raw, 10));
  } catch {
    return DEFAULT_REFRESH_MS;
  }
}

/** Persist an interval; never throws (private mode / disabled storage). */
export function saveRefreshMs(storageKey: string, ms: number): void {
  try {
    localStorage.setItem(storageKey, String(normalizeRefreshMs(ms)));
  } catch {
    /* preference is best-effort */
  }
}
