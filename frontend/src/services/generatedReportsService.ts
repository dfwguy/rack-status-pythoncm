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
import { apiFetch } from "../lib/apiFetch";
import type { GeneratedReport, GeneratedReportsResponse } from "../types/api";

async function jsonOrError<T>(res: Response): Promise<T> {
  const raw = await res.text();
  let data: Record<string, unknown>;
  try {
    data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
  } catch {
    throw new Error(`Invalid JSON (HTTP ${res.status}): ${raw.slice(0, 240)}`);
  }
  if (!res.ok || data.ok === false) {
    throw new Error(typeof data.error === "string" ? data.error : `HTTP ${res.status}`);
  }
  return data as T;
}

/** List the reports persisted by completed fleet report jobs, newest first. */
export async function fetchGeneratedReports(): Promise<GeneratedReportsResponse> {
  const res = await apiFetch("/api/reports/generated", { cache: "no-store" });
  const data = await jsonOrError<GeneratedReportsResponse>(res);
  return {
    ...data,
    reports: Array.isArray(data.reports) ? data.reports : [],
    max_kept: Number.isFinite(data.max_kept) ? data.max_kept : 0,
  };
}

/** Remove one archived report after the caller has confirmed the destructive action. */
export async function deleteGeneratedReport(reportId: string): Promise<void> {
  const res = await apiFetch(`/api/reports/generated/${encodeURIComponent(reportId)}`, {
    method: "DELETE",
  });
  await jsonOrError<{ ok: true }>(res);
}

/** Download exactly the bytes and filename supplied by the report archive endpoint. */
export async function downloadGeneratedReport(report: GeneratedReport): Promise<void> {
  const res = await apiFetch(`/api/reports/generated/${encodeURIComponent(report.id)}`);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = ((await res.json()) as { error?: string }).error || detail;
    } catch {
      // Preserve the status when a proxy returns a non-JSON error page.
    }
    throw new Error(detail);
  }
  const blob = await res.blob();
  const name =
    /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] ||
    report.filename ||
    `report.${report.format || "bin"}`;
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}
