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
 * Per-rack command context object, its hook and the re-exported command types.
 *
 * Componentless so React Refresh can hot-swap the provider (RackCommandProvider.tsx); see
 * ThemeContext.ts. Consumers keep importing `useRackCommands` and the types from this path.
 */
import { createContext, useContext } from "react";
import type { RackCommandsStoreValue } from "./RackCommandsStoreContext";
import type { NodeCmdKind, RackLastAsyncOutput } from "../lib/rackCommandTypes";

export type { NodeCmdKind, RackLastAsyncOutput } from "../lib/rackCommandTypes";
export { validationRackKey } from "../lib/rackCommandTypes";

export interface RackCommandContextValue {
  rack: string;
  logDir: string;
  jobId?: string;
  /** React-started async jobs still running for this rack (for header blink). */
  asyncPendingCount: number;
  rackLastOutput: RackLastAsyncOutput | null;
  openStream: RackCommandsStoreValue["openStream"];
  openText: RackCommandsStoreValue["openText"];
  runSyncJson: (title: string, url: string, body: Record<string, unknown>) => Promise<void>;
  runAsyncReset: (label: string, cmdKind: NodeCmdKind, url: string, body: Record<string, unknown>) => Promise<void>;
  runAsyncValidationFamily: (
    label: string,
    cmdKind: NodeCmdKind,
    postUrl: string,
    statusUrl: (jobId: string) => string,
    body: Record<string, unknown>,
  ) => Promise<void>;
  launchAsyncValidationFamily: (
    label: string,
    cmdKind: NodeCmdKind,
    postUrl: string,
    statusUrl: (jobId: string) => string,
    body: Record<string, unknown>,
  ) => Promise<string>;
  openTailLogNemo: () => void;
}

export const RackCommandContext = createContext<RackCommandContextValue | null>(null);

export function useRackCommands(): RackCommandContextValue {
  const v = useContext(RackCommandContext);
  if (!v) throw new Error("useRackCommands must be used under RackCommandProvider");
  return v;
}
