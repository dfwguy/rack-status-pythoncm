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
 * Operator display name shared across the SPA. Optional — empty when no name is set.
 *
 * Persisted in localStorage so it survives reloads. The Settings → General tab writes
 * it; every API call reads it via {@link apiFetch} and forwards it as the
 * `X-Rack-Operator` header. The server treats it as advisory metadata only (it is **not**
 * an authentication token).
 */

export const OPERATOR_NAME_KEY = "nvis-rack-operator";
export const AUTO_CLAIM_KEY = "nvis-rack-autoclaim";
const MAX_LEN = 64;

/** Read the current operator display name (empty string when not set or storage fails). */
export function getOperatorName(): string {
  try {
    const raw = localStorage.getItem(OPERATOR_NAME_KEY) ?? "";
    return raw.trim().slice(0, MAX_LEN);
  } catch {
    return "";
  }
}

/** Save (or clear when blank) the operator name. */
export function setOperatorName(name: string): void {
  try {
    const v = (name || "").trim().slice(0, MAX_LEN);
    if (v) {
      localStorage.setItem(OPERATOR_NAME_KEY, v);
    } else {
      localStorage.removeItem(OPERATOR_NAME_KEY);
    }
  } catch {
    // ignore quota/permission errors
  }
}

/** True when the user opted into auto-claiming a rack on opening its detail view. */
export function isAutoClaimEnabled(): boolean {
  try {
    return localStorage.getItem(AUTO_CLAIM_KEY) === "1";
  } catch {
    return false;
  }
}

export function setAutoClaimEnabled(enabled: boolean): void {
  try {
    if (enabled) {
      localStorage.setItem(AUTO_CLAIM_KEY, "1");
    } else {
      localStorage.removeItem(AUTO_CLAIM_KEY);
    }
  } catch {
    // ignore
  }
}
