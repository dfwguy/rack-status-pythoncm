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

/** Provider-neutral command details: command line, exit code, stdout, and stderr. */
export function formatSchedulerCommandResult(data: Record<string, unknown>): string {
  const lines: string[] = [];
  if (data.command_display) lines.push(String(data.command_display));
  if (data.returncode != null) lines.push(`exit ${String(data.returncode)}`);
  if (data.stdout) lines.push(`stdout:\n${String(data.stdout)}`);
  if (data.stderr) lines.push(`stderr:\n${String(data.stderr)}`);
  return lines.join("\n").trim();
}

/** Compatibility export for callers and tests written against the original Slurm-only helper. */
export const formatSlurmScontrolResult = formatSchedulerCommandResult;

function isAsyncJobStatusShape(data: Record<string, unknown>): boolean {
  return data.phase !== undefined && data.phase !== null;
}

/**
 * Text for immediate scheduler JSON POST results (drain/resume/cancel, etc.).
 * Legacy Slurm-shaped payloads and PythonCM/direct payloads share the same command/result envelope.
 */
export function formatSyncJsonResult(title: string, rack: string, data: Record<string, unknown>): string {
  if (isAsyncJobStatusShape(data)) {
    return formatJobStatusText(data) || JSON.stringify(data, null, 2);
  }

  const t = title.toLowerCase();
  const looksSchedulerSync =
    /slurm|scheduler|drain|undrain|resume|cancel|scancel/.test(t) &&
    (data.command_display != null ||
      data.returncode != null ||
      data.stdout != null ||
      data.stderr != null);

  if (looksSchedulerSync) {
    const detail = formatSchedulerCommandResult(data);
    const ok = data.ok !== false;
    const err = String(data.error || "").trim();
    const jobId = String(data.job_id ?? "").trim();

    if (!ok) {
      const head = err || "command failed";
      return detail ? `${head}\n\n${detail}` : head;
    }

    let head: string;
    if (t.includes("cancel")) head = `Cancellation completed for job ${jobId || "?"}.`;
    else if (t.includes("undrain") || t.includes("resume")) head = `Resume completed for ${rack}.`;
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
  const tasks = Array.isArray(st.tasks) ? st.tasks as Array<Record<string, unknown>> : [];
  const schedulerIds = [...new Set(tasks
    .map((task) => String(task.scheduler_ref ?? "").trim())
    .filter(Boolean))];
  const jobId = String(st.job_id ?? "").trim();
  if (/^\d+$/.test(jobId) && !schedulerIds.includes(jobId)) schedulerIds.unshift(jobId);
  if (schedulerIds.length) lines.push(`Scheduler Job ID${schedulerIds.length === 1 ? "" : "s"}: ${schedulerIds.join(", ")}`);
  const activityId = String(st.activity_id ?? (!/^\d+$/.test(jobId) ? jobId : "")).trim();
  if (activityId) lines.push(`Activity ID: ${activityId}`);
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
  const stdout = st.stdout ?? st.stdout_tail;
  const stderr = st.stderr ?? st.stderr_tail;
  if (stdout) lines.push(`stdout:\n${String(stdout)}`);
  if (stderr) lines.push(`stderr:\n${String(stderr)}`);
  if (st.reset_sleep_sec != null) lines.push(`reset_sleep_sec: ${String(st.reset_sleep_sec)}`);
  if (st.nvlmapper_zip) lines.push(`nvlmapper_zip: ${String(st.nvlmapper_zip)}`);
  if (st.nvlmapper_work_dir) lines.push(`nvlmapper_work_dir (CSV/XLS on disk): ${String(st.nvlmapper_work_dir)}`);
  return lines.join("\n").trim();
}
