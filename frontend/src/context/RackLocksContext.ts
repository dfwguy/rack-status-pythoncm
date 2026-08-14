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
 * Single-instance ``useRackLocks`` poller exposed via React context so every consumer
 * (table icons, banner, conflict dialog interceptor) shares the same 5 s snapshot.
 *
 * Context object and hook only — the provider component lives in RackLocksProvider.tsx so that
 * React Refresh can hot-swap it. See ThemeContext.ts for the reasoning.
 */
import { createContext, useContext } from "react";
import type { UseRackLocksResult } from "../hooks/useRackLocks";

export const RackLocksContext = createContext<UseRackLocksResult | null>(null);

export function useRackLocksContext(): UseRackLocksResult {
  const v = useContext(RackLocksContext);
  if (!v) {
    throw new Error("useRackLocksContext must be used under RackLocksProvider");
  }
  return v;
}
