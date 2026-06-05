import {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
  type ReactNode,
} from "react";
import { streamChat, type ChatMessagePayload, type PageContext } from "../services/aiService";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  streaming?: boolean;
}

interface AIChatState {
  messages: ChatMessage[];
  isStreaming: boolean;
  pageContext: PageContext;
  isOpen: boolean;
  isMinimized: boolean;
}

interface AIChatActions {
  sendMessage: (content: string) => void;
  clearHistory: () => void;
  stopStreaming: () => void;
  setOpen: (open: boolean) => void;
  setMinimized: (minimized: boolean) => void;
  setPageContext: (ctx: PageContext) => void;
}

type AIChatContextType = AIChatState & AIChatActions;

const AIChatCtx = createContext<AIChatContextType | null>(null);

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
