import { apiFetch } from "./apiFetch";
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
/** True when validation row indicates a live NEMO job (same idea as legacy tail vs open log). */
export function validationOngoing(nemo: string): boolean {
  return nemo.trim().toLowerCase().startsWith("ongoing");
}

export type OpenRackLogParams =
  | { rack: string; job_id?: string }
  | { log_file: string };

/**
 * Fetch the resolved NEMO rack / multirack log and open it in a new tab as plain text (GET /api/rack-log-download).
 */
export async function openRackLogInNewTab(
  logDir: string | undefined,
  params: OpenRackLogParams
): Promise<{ ok: true } | { ok: false; error: string }> {
  const qs = new URLSearchParams();
  const d = (logDir ?? "").trim();
  if (d) qs.set("log_dir", d);
  if ("log_file" in params) {
    const lf = params.log_file.trim();
    if (!lf) return { ok: false, error: "No log file selected." };
    qs.set("log_file", lf);
  } else {
    qs.set("rack", params.rack);
    const j = params.job_id?.trim();
    if (j && j !== "-") qs.set("job_id", j);
  }

  const res = await apiFetch(`/api/rack-log-download?${qs.toString()}`, { credentials: "same-origin" });
  if (res.status === 401) {
    // apiFetch already tried a renewal and, on failure, signed the UI out (the route is kept).
    return { ok: false, error: "Authentication required." };
  }
  const ctype = (res.headers.get("Content-Type") || "").toLowerCase();
  if (!res.ok) {
    if (ctype.includes("application/json")) {
      const j = (await res.json().catch(() => ({}))) as { error?: string };
      return { ok: false, error: j.error || `Download failed (HTTP ${res.status}).` };
    }
    const t = await res.text();
    return { ok: false, error: t.slice(0, 400) || `Download failed (HTTP ${res.status}).` };
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(new Blob([blob], { type: "text/plain;charset=utf-8" }));
  window.open(url, "_blank", "noopener,noreferrer");
  setTimeout(() => URL.revokeObjectURL(url), 120_000);
  return { ok: true };
}

/** JSON body for POST /api/tail-log-stream (matches Flask `_parse_tail_log_request`). */
export type TailLogStreamBody = {
  log_dir?: string;
  rack?: string;
  job_id?: string;
  log_file?: string;
};

/** JSON body for POST /api/tail-job-stdout-stream (``path`` from ``scontrol show job`` ``StdOut=``). */
export type JobStdoutTailPayload = {
  path: string;
  log_dir?: string;
};

export function buildSingleRackTailBody(
  logDir: string | undefined,
  rack: string,
  jobId: string | undefined
): TailLogStreamBody {
  const body: TailLogStreamBody = {};
  const d = (logDir ?? "").trim();
  if (d) body.log_dir = d;
  body.rack = rack;
  const j = jobId?.trim();
  if (j && j !== "-") body.job_id = j;
  return body;
}

export function buildMultirackTailBody(logDir: string | undefined, logBasename: string): TailLogStreamBody {
  const body: TailLogStreamBody = {};
  const d = (logDir ?? "").trim();
  if (d) body.log_dir = d;
  body.log_file = logBasename.trim();
  return body;
}
