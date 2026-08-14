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
 * Classify a Slurm state/reason string (squeue NODELIST(REASON), sinfo STATE/REASON)
 * into a severity tone so the UI can highlight failures/holds at a glance.
 */
export type SlurmTone = "error" | "warning" | "none";

const ERROR_RE = /(fail|error|down|invalid|unkillable|not\s*responding|kill\s*task|bad|nhc)/i;
const WARNING_RE = /(drain|held|requeued|reboot|maint|reserved|prolog|epilog|stopped|completing|power|boot|update)/i;

export function slurmReasonTone(text: string | null | undefined): SlurmTone {
  const t = String(text ?? "").trim();
  if (!t || t === "none" || t === "None") return "none";
  if (ERROR_RE.test(t)) return "error";
  if (WARNING_RE.test(t)) return "warning";
  return "none";
}

/** CSS color for a tone, or undefined to inherit. */
export function slurmToneColor(tone: SlurmTone): string | undefined {
  if (tone === "error") return "var(--text-color-error, #f44336)";
  if (tone === "warning") return "#ed6c02";
  return undefined;
}
