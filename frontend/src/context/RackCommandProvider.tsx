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
/** Binds one rack's command actions to the shared store; hook lives in RackCommandContext.ts. */
import { useMemo, useCallback, type ReactNode } from "react";
import { useRackCommandsStore } from "./RackCommandsStoreContext";
import type { NodeCmdKind } from "../lib/rackCommandTypes";
import { RackCommandContext, type RackCommandContextValue } from "./RackCommandContext";


export function RackCommandProvider({
  rack,
  logDir,
  jobId,
  children,
}: {
  rack: string;
  logDir?: string;
  jobId?: string;
  children: ReactNode;
}) {
  const store = useRackCommandsStore();
  const ld = (logDir ?? "").trim();
  const jid = jobId && jobId !== "-" ? jobId : undefined;

  const rackLastOutput = store.getRackLast(rack);
  const pendingForRack = store.getPendingForRack(rack);

  const runSyncJson = useCallback(
    (title: string, url: string, body: Record<string, unknown>) => store.runSyncJson(rack, title, url, body),
    [store, rack]
  );

  const runAsyncReset = useCallback(
    (label: string, cmdKind: NodeCmdKind, url: string, body: Record<string, unknown>) =>
      store.runAsyncReset(rack, label, cmdKind, url, body),
    [store, rack]
  );

  const runAsyncValidationFamily = useCallback(
    (label: string, cmdKind: NodeCmdKind, postUrl: string, statusUrl: (jobId: string) => string, body: Record<string, unknown>) =>
      store.runAsyncValidationFamily(rack, label, cmdKind, postUrl, statusUrl, body),
    [store, rack]
  );

  const launchAsyncValidationFamily = useCallback(
    (label: string, cmdKind: NodeCmdKind, postUrl: string, statusUrl: (jobId: string) => string, body: Record<string, unknown>) =>
      store.launchAsyncValidationFamily(rack, label, cmdKind, postUrl, statusUrl, body),
    [store, rack]
  );

  const openTailLogNemo = useCallback(() => store.openTailLogNemo(rack, ld, jid), [store, rack, ld, jid]);

  const value = useMemo<RackCommandContextValue>(
    () => ({
      rack,
      logDir: ld,
      jobId: jid,
      asyncPendingCount: pendingForRack.length,
      rackLastOutput,
      openStream: store.openStream,
      openText: store.openText,
      runSyncJson,
      runAsyncReset,
      runAsyncValidationFamily,
      launchAsyncValidationFamily,
      openTailLogNemo,
    }),
    [
      rack,
      ld,
      jid,
      pendingForRack.length,
      rackLastOutput,
      store.openStream,
      store.openText,
      runSyncJson,
      runAsyncReset,
      runAsyncValidationFamily,
      launchAsyncValidationFamily,
      openTailLogNemo,
    ]
  );

  return <RackCommandContext.Provider value={value}>{children}</RackCommandContext.Provider>;
}
