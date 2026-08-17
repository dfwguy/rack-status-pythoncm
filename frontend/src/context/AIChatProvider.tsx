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
/** AI chat provider: owns the message list and the streaming request. */
import {
  useState,
  useCallback,
  useRef,
  type ReactNode,
} from "react";
import {
  streamChat,
  resumeChat,
  type ChatMessagePayload,
  type PageContext,
  type StreamHandlers,
  type ToolEvent,
} from "../services/aiService";

import { AIChatCtx, type ChatMessage, type PendingApproval } from "./AIChatContext";


let msgCounter = 0;
function nextMsgId(): string {
  return `msg-${Date.now()}-${++msgCounter}`;
}

export function AIChatProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isOpen, setOpen] = useState(false);
  const [isMinimized, setMinimized] = useState(false);
  const [pageContext, setPageContext] = useState<PageContext>({ page: "dashboard" });
  const abortRef = useRef<AbortController | null>(null);

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsStreaming(false);
    setMessages((prev) =>
      prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
    );
  }, []);

  // One set of SSE handlers that update a specific assistant message. Shared by the
  // initial send and by resume (after an approval) so tool activity + tokens keep
  // flowing into the same message bubble.
  const makeHandlers = useCallback((assistantId: string): StreamHandlers => ({
    onChunk: (chunk) => {
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + chunk } : m)),
      );
    },
    onEvent: (ev: ToolEvent) => {
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== assistantId) return m;
          if (ev.type === "approval_required") {
            return {
              ...m,
              approval: {
                run_id: ev.run_id || "",
                call_id: ev.call_id || "",
                tool: ev.tool,
                category: ev.category,
                args: ev.args || {},
                summary: ev.summary || ev.tool,
              },
            };
          }
          const tools = [...(m.tools || [])];
          if (ev.type === "tool_call") {
            tools.push({ tool: ev.tool, category: ev.category, args: ev.args, status: "running" });
          } else if (ev.type === "tool_result") {
            // resolve the most recent running entry for this tool
            for (let i = tools.length - 1; i >= 0; i--) {
              if (tools[i].tool === ev.tool && tools[i].status === "running") {
                tools[i] = { ...tools[i], status: "done", ok: ev.ok, summary: ev.summary };
                return { ...m, tools };
              }
            }
            tools.push({ tool: ev.tool, status: "done", ok: ev.ok, summary: ev.summary });
          }
          return { ...m, tools };
        }),
      );
    },
    onDone: () => {
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m)));
      setIsStreaming(false);
      abortRef.current = null;
    },
    onError: (error) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                content: m.content ? `${m.content}\n\nError: ${error}` : `Error: ${error}`,
                streaming: false,
              }
            : m,
        ),
      );
      setIsStreaming(false);
      abortRef.current = null;
    },
  }), []);

  const sendMessage = useCallback(
    (content: string) => {
      if (isStreaming || !content.trim()) return;

      const userMsg: ChatMessage = {
        id: nextMsgId(),
        role: "user",
        content: content.trim(),
        timestamp: new Date(),
      };

      const assistantMsg: ChatMessage = {
        id: nextMsgId(),
        role: "assistant",
        content: "",
        timestamp: new Date(),
        streaming: true,
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setIsStreaming(true);

      const history: ChatMessagePayload[] = [
        ...messages.map((m) => ({ role: m.role, content: m.content })),
        { role: "user" as const, content: content.trim() },
      ];

      abortRef.current = streamChat(history, pageContext, makeHandlers(assistantMsg.id));
    },
    [isStreaming, messages, pageContext, makeHandlers],
  );

  const resolveApproval = useCallback(
    (messageId: string, decision: "approve" | "deny") => {
      let approval: PendingApproval | null = null;
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== messageId) return m;
          approval = m.approval || null;
          return { ...m, approval: null, streaming: true };
        }),
      );
      if (!approval) return;
      const { run_id, call_id } = approval;
      setIsStreaming(true);
      abortRef.current = resumeChat({ run_id, call_id, decision }, makeHandlers(messageId));
    },
    [makeHandlers],
  );

  const clearHistory = useCallback(() => {
    stopStreaming();
    setMessages([]);
  }, [stopStreaming]);

  return (
    <AIChatCtx.Provider
      value={{
        messages,
        isStreaming,
        pageContext,
        isOpen,
        isMinimized,
        sendMessage,
        clearHistory,
        stopStreaming,
        resolveApproval,
        setOpen,
        setMinimized,
        setPageContext,
      }}
    >
      {children}
    </AIChatCtx.Provider>
  );
}
