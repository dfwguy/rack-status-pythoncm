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
// logRowForActivity.ts — match a validation activity to the parser log that belongs to it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Lives outside ValidationActivitiesTable.tsx because a file that exports a component may not
// also export functions: React Fast Refresh cannot hot-reload such a module, and eslint's
// react-refresh/only-export-components enforces it as a hard error in CI.
import type { ValidationActivity } from "../../types/api";

export interface ValidationActivitiesLogRow {
  log_file: string;
  display_rack: string;
  last_job: string;
}

/** Scheduler job ids this activity owns, most specific first: its tasks, then its own job_id. */
function activityJobIds(activity: ValidationActivity): string[] {
  const out: string[] = [];
  for (const t of activity.tasks ?? []) {
    const ref = String(t.scheduler_ref ?? "").trim();
    if (ref && !out.includes(ref)) out.push(ref);
  }
  const own = String(activity.job_id ?? "").trim();
  // job_id is a UUID for dashboard-launched activities and a Slurm id only for adopted ones.
  if (/^\d+$/.test(own) && !out.includes(own)) out.push(own);
  return out;
}

/** Rack tokens in a log row, from the display label and the "+"-joined filename. */
function logRowRacks(row: ValidationActivitiesLogRow): Set<string> {
  const hay = `${row.display_rack} ${row.log_file}`.toLowerCase();
  return new Set(hay.match(/[a-z]+\d+-[a-z]\d+/g) ?? []);
}

/**
 * The parser log belonging to an activity.
 *
 * Job id first: rack-name matching cannot distinguish a run from an OLDER run whose rack set is a
 * superset of it. A 4-rack NEMO activity on h18-h21 scored 4 against its own log AND against a
 * stale 5-rack h18-h22 NCCL log, and the tie was broken by listing order — so Charts opened the
 * wrong job's results (job 131920 showing 127207). Only fall back to racks when no scheduler id
 * is known, and then require an exact rack-set match before accepting a superset.
 */
export function logRowForActivity(
  activity: ValidationActivity,
  logRows: ValidationActivitiesLogRow[] | undefined,
): ValidationActivitiesLogRow | null {
  if (!logRows?.length) return null;

  for (const jid of activityJobIds(activity)) {
    const exact = logRows.find((r) => r.last_job === jid || r.log_file.includes(jid));
    if (exact) return exact;
  }

  const keys = [activity.rack, ...activity.racks].map((r) => r.toLowerCase()).filter(Boolean);
  if (keys.length === 0) return null;
  const wanted = new Set(keys);

  // Exact rack-set match wins outright over any superset.
  const exactSet = logRows.find((row) => {
    const got = logRowRacks(row);
    return got.size === wanted.size && [...wanted].every((k) => got.has(k));
  });
  if (exactSet) return exactSet;

  // Otherwise best rack coverage. Ties are real (an older superset run covers all our racks), and
  // the backend sorts candidates alphabetically by filename — not by time — so list order carries
  // no recency information. Break ties on the highest numeric job id, which does.
  let best: ValidationActivitiesLogRow | null = null;
  let bestScore = 0;
  let bestJob = -1;
  for (const row of logRows) {
    const hay = `${row.display_rack} ${row.log_file}`.toLowerCase();
    let score = 0;
    for (const k of keys) if (hay.includes(k)) score++;
    if (score === 0) continue;
    const jobNum = /^\d+$/.test(row.last_job) ? Number(row.last_job) : -1;
    if (score > bestScore || (score === bestScore && jobNum > bestJob)) {
      bestScore = score;
      bestJob = jobNum;
      best = row;
    }
  }
  return best;
}
