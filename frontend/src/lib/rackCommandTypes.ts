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
export type NodeCmdKind =
  | "validation"
  | "scheduler"
  /** Persisted compatibility value from Slurm-era command history. */
  | "slurm"
  | "stress"
  | "power"
  | "gpu"
  | "services"
  | "hw"
  | "nmxc"
  | "bmc"
  | "mac";

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
