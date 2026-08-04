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
  openTailLogNemo: (rack: string, logDir: string, jobId?: string) => void;
}

export const RackCommandsStoreContext = createContext<RackCommandsStoreValue | null>(null);

export function useRackCommandsStore(): RackCommandsStoreValue {
  const v = useContext(RackCommandsStoreContext);
  if (!v) throw new Error("useRackCommandsStore must be used under RackCommandsStoreProvider");
  return v;
}
