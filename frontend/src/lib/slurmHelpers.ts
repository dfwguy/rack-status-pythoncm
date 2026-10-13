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
import { apiFetch } from "./apiFetch";
/** Slurm job id suitable for scancel (digits, optional _task for job arrays). Legacy-aligned. */
export function slurmJobIdForScancel(raw: unknown): string {
  const t = String(raw ?? "").trim();
  return /^[1-9]\d*(_\d+)?$/.test(t) ? t : "";
}

/** Parse ``StdOut=`` from ``scontrol show job`` stdout (multiline). */
export function parseSlurmStdOutPathFromScontrolText(text: string): string {
  const m = /^\s*StdOut\s*=\s*(.+)$/im.exec(String(text || ""));
  if (!m) return "";
  const p = String(m[1] || "").trim();
  if (!p || /^\(none\)$/i.test(p) || /^\/dev\/null$/i.test(p)) return "";
  return p;
}

/**
 * ``JobState=`` and ``Reason=`` from ``scontrol show job`` stdout. A PENDING job has no log yet --
 * Slurm creates its StdOut file when the job starts -- so a view can say that instead of failing to
 * open a path that does not exist (job 194782, queued behind another 36-node run).
 */
export function parseSlurmJobStateFromScontrolText(text: string): { state: string; reason: string } {
  const t = String(text || "");
  const state = /\bJobState=(\S+)/.exec(t)?.[1] ?? "";
  const reason = /\bReason=(\S+)/.exec(t)?.[1] ?? "";
  return { state: state.toUpperCase(), reason: reason === "None" ? "" : reason.replace(/_/g, " ") };
}

/**
 * squeue's job state codes (``%t``, the ``ST`` column) in words. ``PD`` and ``R`` read as noise next
 * to a node list that is sometimes a reason and sometimes hosts; "Pending" / "Running" say which.
 * From squeue(1) JOB STATE CODES.
 */
const SLURM_JOB_STATE_WORDS: Record<string, string> = {
  BF: "Boot fail",
  CA: "Cancelled",
  CD: "Completed",
  CF: "Configuring",
  CG: "Completing",
  DL: "Deadline",
  F: "Failed",
  NF: "Node fail",
  OOM: "Out of memory",
  PD: "Pending",
  PR: "Preempted",
  R: "Running",
  RD: "Held (reservation deleted)",
  RF: "Requeue federation",
  RH: "Requeue held",
  RQ: "Requeued",
  RS: "Resizing",
  RV: "Revoked",
  SE: "Special exit",
  SI: "Signaling",
  SO: "Staging out",
  ST: "Stopped",
  S: "Suspended",
  TO: "Timeout",
};

/**
 * A job state in words: a squeue code (``PD``) or a long name (``PENDING``, ``NODE_FAIL``) becomes
 * "Pending" / "Node fail"; anything else is shown as Slurm wrote it.
 */
export function slurmJobStateLabel(raw: string | null | undefined): string {
  const t = String(raw ?? "").trim();
  if (!t) return "";
  const word = SLURM_JOB_STATE_WORDS[t.toUpperCase()];
  if (word) return word;
  if (/^[A-Z_]{3,}$/.test(t)) {
    const words = t.toLowerCase().replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
  return t;
}

export type SlurmSacctJob = {
  job_id: string;
  name: string;
  partition: string;
  nnodes: string;
  nodelist: string;
  start: string;
  end: string;
  elapsed: string;
  state: string;
  exit_code: string;
};

export type SlurmCmdJson = {
  ok?: boolean;
  error?: string;
  stdout?: string;
  stderr?: string;
  command_display?: string;
  returncode?: number;
  job_id?: string;
  /** /api/scheduler-history */
  jobs?: SlurmSacctJob[];
  /** /api/scheduler-job-log-path */
  path?: string;
  source?: string;
};

export function formatSlurmCmdDetail(data: SlurmCmdJson): string {
  const lines: string[] = [];
  if (data.command_display) lines.push(String(data.command_display));
  lines.push(`exit ${data.returncode ?? "?"}`);
  if (data.stdout) lines.push(`stdout:\n${data.stdout}`);
  if (data.stderr) lines.push(`stderr:\n${data.stderr}`);
  return lines.join("\n").trim();
}

export async function fetchSlurmJson(url: string, init?: RequestInit): Promise<{ res: Response; data: SlurmCmdJson }> {
  const res = await apiFetch(url, init);
  const raw = await res.text();
  // No initialiser: the catch throws, so `data` is either parsed or we never reach the use.
  let data: SlurmCmdJson;
  try {
    data = raw ? (JSON.parse(raw) as SlurmCmdJson) : {};
  } catch {
    throw new Error(`Invalid JSON (HTTP ${res.status}): ${raw.slice(0, 300)}`);
  }
  return { res, data };
}

export function findSqueueJobColumnIndex(headers: string[]): number {
  for (let hi = 0; hi < headers.length; hi++) {
    const h = String(headers[hi] || "")
      .trim()
      .toUpperCase()
      .replace(/\s+/g, "_");
    if (h === "JOBID" || h === "JOB_ID") return hi;
  }
  return -1;
}
