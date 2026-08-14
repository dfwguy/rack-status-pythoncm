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
/** Provider for the shared rack-locks poller; the hook lives in RackLocksContext.ts. */
import type { ReactNode } from "react";
import { useRackLocks } from "../hooks/useRackLocks";
import { RackLocksContext } from "./RackLocksContext";

export function RackLocksProvider({ children }: { children: ReactNode }) {
  const value = useRackLocks();
  return <RackLocksContext.Provider value={value}>{children}</RackLocksContext.Provider>;
}
