import { createContext, useContext, useMemo, useCallback, type ReactNode } from "react";
import { useRackCommandsStore, type RackCommandsStoreValue } from "./RackCommandsStoreContext";
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
  openTailLogNemo: () => void;
}

const RackCommandContext = createContext<RackCommandContextValue | null>(null);

export function useRackCommands(): RackCommandContextValue {
  const v = useContext(RackCommandContext);
  if (!v) throw new Error("useRackCommands must be used under RackCommandProvider");
  return v;
}

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
      openTailLogNemo,
    ]
  );

  return <RackCommandContext.Provider value={value}>{children}</RackCommandContext.Provider>;
}
