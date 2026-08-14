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
import { streamChat, type ChatMessagePayload, type PageContext } from "../services/aiService";

import { AIChatCtx, type ChatMessage } from "./AIChatContext";


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

      streamChat(
        history,
        pageContext,
        (chunk) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsg.id
                ? { ...m, content: m.content + chunk }
                : m,
            ),
          );
        },
        () => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsg.id ? { ...m, streaming: false } : m,
            ),
          );
          setIsStreaming(false);
          abortRef.current = null;
        },
        (error) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsg.id
                ? { ...m, content: `Error: ${error}`, streaming: false }
                : m,
            ),
          );
          setIsStreaming(false);
          abortRef.current = null;
        },
      ).then((ctrl) => {
        abortRef.current = ctrl;
      });
    },
    [isStreaming, messages, pageContext],
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
        setOpen,
        setMinimized,
        setPageContext,
      }}
    >
      {children}
    </AIChatCtx.Provider>
  );
}
