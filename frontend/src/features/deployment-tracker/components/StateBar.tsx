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
import type { StateCounts } from "../types";
import { STATE_COLOR, STATE_LABEL, formatCount, segments } from "../lib/trackerModel";

interface StateBarProps {
  counts: StateCounts;
  /** Bar thickness in px. */
  height?: number;
  label: string;
}

/**
 * A stacked bar of deployment states. KUI's `ProgressBar` draws one value; this needs five, so
 * it is drawn here -- with the same track token, so it sits beside a `ProgressBar` unnoticed.
 */
export function StateBar({ counts, height = 8, label }: StateBarProps) {
  const parts = segments(counts);
  const summary = parts.map((p) => `${formatCount(p.count)} ${STATE_LABEL[p.state].toLowerCase()}`).join(", ");
  return (
    <div
      role="img"
      aria-label={`${label}: ${summary || "no nodes"}`}
      style={{
        display: "flex",
        width: "100%",
        height,
        borderRadius: height / 2,
        overflow: "hidden",
        background: "var(--background-color-component-track)",
      }}
    >
      {parts.map((p) => (
        <div key={p.state} style={{ width: `${p.share}%`, background: STATE_COLOR[p.state] }} />
      ))}
    </div>
  );
}
