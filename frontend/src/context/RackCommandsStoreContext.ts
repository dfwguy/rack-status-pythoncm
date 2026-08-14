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
 * Shared rack-command store: context object, value type and hook.
 *
 * Componentless so React Refresh can hot-swap the provider, which now lives in
 * RackCommandsStoreProvider.tsx. See ThemeContext.ts for the reasoning.
 */
import { createContext, useContext } from "react";
import type { NodeCmdKind, RackLastAsyncOutput } from "../lib/rackCommandTypes";

export interface RackCommandsStoreValue {
  /** Last finished command text for a rack (normalized key). */
  getRackLast: (rack: string) => RackLastAsyncOutput | null;
  /** In-flight jobs started from the React UI for this rack. */
  getPendingForRack: (rack: string) => Array<{ id: string; label: string }>;
  replayLastOutput: (rack: string) => void;
  openText: (title: string, text: string, isErr?: boolean) => void;
  openStream: (title: string, endpoint: string, body: Record<string, unknown>) => Promise<void>;
  runSyncJson: (rack: string, title: string, url: string, body: Record<string, unknown>) => Promise<void>;
  runAsyncReset: (rack: string, label: string, cmdKind: NodeCmdKind, url: string, body: Record<string, unknown>) => Promise<void>;
  runAsyncValidationFamily: (
    rack: string,
    label: string,
    cmdKind: NodeCmdKind,
    postUrl: string,
    statusUrl: (jobId: string) => string,
    body: Record<string, unknown>,
  ) => Promise<void>;
  /** Submit a validation and return once the server accepts it; completion polling continues in the store. */
  launchAsyncValidationFamily: (
    rack: string,
    label: string,
    cmdKind: NodeCmdKind,
    postUrl: string,
    statusUrl: (jobId: string) => string,
    body: Record<string, unknown>,
  ) => Promise<string>;
  openTailLogNemo: (rack: string, logDir: string, jobId?: string) => void;
}

export const RackCommandsStoreContext = createContext<RackCommandsStoreValue | null>(null);

export function useRackCommandsStore(): RackCommandsStoreValue {
  const v = useContext(RackCommandsStoreContext);
  if (!v) throw new Error("useRackCommandsStore must be used under RackCommandsStoreProvider");
  return v;
}
