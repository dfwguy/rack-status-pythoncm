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
 * AI chat context object, value types and hooks.
 *
 * Componentless so React Refresh can hot-swap the provider (AIChatProvider.tsx); see
 * ThemeContext.ts. Consumers keep importing the hooks from this path.
 */
import { createContext, useContext } from "react";
import type { PageContext } from "../services/aiService";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  streaming?: boolean;
}

export interface AIChatState {
  messages: ChatMessage[];
  isStreaming: boolean;
  pageContext: PageContext;
  isOpen: boolean;
  isMinimized: boolean;
}

export interface AIChatActions {
  sendMessage: (content: string) => void;
  clearHistory: () => void;
  stopStreaming: () => void;
  setOpen: (open: boolean) => void;
  setMinimized: (minimized: boolean) => void;
  setPageContext: (ctx: PageContext) => void;
}

export type AIChatContextType = AIChatState & AIChatActions;

export const AIChatCtx = createContext<AIChatContextType | null>(null);

export function useAIChat(): AIChatContextType {
  const ctx = useContext(AIChatCtx);
  if (!ctx) throw new Error("useAIChat must be used within AIChatProvider");
  return ctx;
}

/**
 * Like `useAIChat`, but returns null instead of throwing when no provider is
 * present. Used by always-on chrome (e.g. PageHeader) that may render in
 * isolation (tests) or outside the authenticated app shell.
 */
export function useOptionalAIChat(): AIChatContextType | null {
  return useContext(AIChatCtx);
}
