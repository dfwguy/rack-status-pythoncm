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
/** Format power reset `results[]` entries for display (aligned with legacy formatPowerResults). */
export function formatPowerResults(results: unknown): string {
  if (!Array.isArray(results)) return "";
  const lines: string[] = [];
  for (const r of results as Array<Record<string, unknown>>) {
    const phase = r.phase ? `[${r.phase}] ` : "";
    if (r.command) lines.push(`${phase}${r.command}`);
    lines.push(`${phase}exit ${r.returncode ?? "?"}`);
    if (r.stdout) lines.push(`${phase}stdout: ${r.stdout}`);
    if (r.stderr) lines.push(`${phase}stderr: ${r.stderr}`);
    lines.push("");
  }
  return lines.join("\n").trim();
}

/** Same as legacy `formatSlurmScontrolResult` — command line, exit code, stdout/stderr. */
export function formatSlurmScontrolResult(data: Record<string, unknown>): string {
  const lines: string[] = [];
  if (data.command_display) lines.push(String(data.command_display));
  if (data.returncode != null) lines.push(`exit ${String(data.returncode)}`);
  if (data.stdout) lines.push(`stdout:\n${String(data.stdout)}`);
  if (data.stderr) lines.push(`stderr:\n${String(data.stderr)}`);
  return lines.join("\n").trim();
}

function isAsyncJobStatusShape(data: Record<string, unknown>): boolean {
  return data.phase !== undefined && data.phase !== null;
}

/**
 * Text for immediate JSON POST results (Slurm drain/undrain/scancel, etc.).
 * Slurm sync payloads match legacy `formatSlurmScontrolResult` + completion line; async jobs keep `formatJobStatusText`.
 */
export function formatSyncJsonResult(title: string, rack: string, data: Record<string, unknown>): string {
  if (isAsyncJobStatusShape(data)) {
    return formatJobStatusText(data) || JSON.stringify(data, null, 2);
  }

  const t = title.toLowerCase();
  const looksSlurmSync =
    t.includes("slurm") &&
    (data.command_display != null ||
      data.returncode != null ||
      data.stdout != null ||
      data.stderr != null);

  if (looksSlurmSync) {
    const detail = formatSlurmScontrolResult(data);
    const ok = data.ok !== false;
    const err = String(data.error || "").trim();
    const jobId = String(data.job_id ?? "").trim();

    if (!ok) {
      const head = err || "command failed";
      return detail ? `${head}\n\n${detail}` : head;
    }

    let head: string;
    if (t.includes("scancel")) head = `scancel completed for job ${jobId || "?"}.`;
    else if (t.includes("undrain")) head = `Undrain completed for ${rack}.`;
    else if (t.includes("drain")) head = `Drain completed for ${rack}.`;
    else head = `${title.trim()} completed for ${rack}.`;

    return detail ? `${head}\n\n${detail}` : head;
  }

  return formatJobStatusText(data) || JSON.stringify(data, null, 2);
}

/** Flatten reset-job / validation-job status JSON into readable text for a popup. */
export function formatJobStatusText(st: Record<string, unknown>): string {
  const lines: string[] = [];
  const phase = String(st.phase ?? "?");
  const kind = String(st.kind ?? "").toLowerCase();
  lines.push(`[${phase}] ${kind || "job"}`);
  if (st.command_display) lines.push(String(st.command_display));
  if (st.returncode != null) lines.push(`exit ${String(st.returncode)}`);
  if (st.target) lines.push(`target: ${String(st.target)}`);
  if (st.nvswitch_host) lines.push(`nvswitch_host: ${String(st.nvswitch_host)}`);
  if (st.preset) lines.push(`preset: ${String(st.preset)}`);
  if (st.checker_args) lines.push(`checker_args: ${String(st.checker_args)}`);
  if (st.error) lines.push(`error: ${String(st.error)}`);
  if (Array.isArray(st.results) && st.results.length) {
    lines.push(formatPowerResults(st.results));
  }
  if (st.stdout) lines.push(`stdout:\n${String(st.stdout)}`);
  if (st.stderr) lines.push(`stderr:\n${String(st.stderr)}`);
  if (st.reset_sleep_sec != null) lines.push(`reset_sleep_sec: ${String(st.reset_sleep_sec)}`);
  if (st.nvlmapper_zip) lines.push(`nvlmapper_zip: ${String(st.nvlmapper_zip)}`);
  if (st.nvlmapper_work_dir) lines.push(`nvlmapper_work_dir (CSV/XLS on disk): ${String(st.nvlmapper_work_dir)}`);
  return lines.join("\n").trim();
}
