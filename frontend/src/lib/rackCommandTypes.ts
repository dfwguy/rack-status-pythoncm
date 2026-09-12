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
export const NODE_CMD_KINDS = [
  "validation",
  "slurm",
  "stress",
  "power",
  "gpu",
  "services",
  "hw",
  "nmxc",
  "bmc",
  "mac",
] as const;

export type NodeCmdKind = (typeof NODE_CMD_KINDS)[number];

/**
 * Narrow an untrusted family name, falling back to "validation".
 *
 * Needed since catalog Tool documents started naming the family a launch is filed under: that
 * value is operator-editable and arrives as a plain string, while the palette indexes this
 * union exhaustively. Filing an unrecognised family under "validation" costs the right dot
 * colour; letting it through unchecked is an undefined lookup at render.
 */
export function asNodeCmdKind(value: string | undefined): NodeCmdKind {
  return (NODE_CMD_KINDS as readonly string[]).includes(value ?? "")
    ? (value as NodeCmdKind)
    : "validation";
}

/** Client-only replay buffer per rack (legacy `rackLastAsyncOutput`). */
export interface RackLastAsyncOutput {
  cmdKind: NodeCmdKind;
  isErr: boolean;
  preview: string;
  full: string;
  at: number;
  noticeTitle: string;
}

export function validationRackKey(rack: string): string {
  return (rack || "").trim().toLowerCase();
}
