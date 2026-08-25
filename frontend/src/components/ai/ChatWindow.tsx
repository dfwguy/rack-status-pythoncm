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
import { useState, useRef, useEffect, useCallback } from "react";
import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import { Text } from "@nvidia/foundations-react-core";
import { Send, Square, Trash2 } from "lucide-react";
import { useAIChat } from "../../context/AIChatContext";
import { getContent } from "../../content";
import ChatMessage from "./ChatMessage";

const PAGE_LABELS: Record<string, string> = {
  dashboard: "Dashboard",
  singleRack: "Single Rack",
  multirack: "Multirack",
  scheduler: "Scheduler",
  reports: "Reports",
  analytics: "Analytics",
  tools: "Utility",
  settings: "Settings",
  workflows: "Workflows",
};

export default function ChatWindow() {
  const { messages, isStreaming, pageContext, sendMessage, clearHistory, stopStreaming } =
    useAIChat();
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSend = () => {
    if (!input.trim() || isStreaming) return;
    sendMessage(input);
    setInput("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const contextLabel =
    getContent(pageContext.page)?.title || PAGE_LABELS[pageContext.page] || pageContext.page;

  return (
    <Box
      role="region"
      aria-label="AI assistant chat"
      sx={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}
    >
      <Box
        sx={{
          px: 2,
          py: 0.75,
          bgcolor: "action.hover",
          borderBottom: "1px solid",
          borderColor: "divider",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexShrink: 0,
        }}
      >
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          Context: <strong>{contextLabel}</strong>
          {pageContext.rack && ` (${pageContext.rack})`}
        </Text>
        {messages.length > 0 && (
          <Tooltip title="Clear chat">
            <IconButton size="small" onClick={clearHistory} disabled={isStreaming}>
              <Trash2 size={14} />
            </IconButton>
          </Tooltip>
        )}
      </Box>

      <Box
        sx={{
          flex: 1,
          overflow: "auto",
          py: 1,
          display: "flex",
          flexDirection: "column",
          gap: 0.5,
        }}
      >
        {messages.length === 0 && (
          <Box sx={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", p: 3 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", textAlign: "center" }}>
              Ask me anything about rack status, validation, PythonCM direct execution, or troubleshooting.
            </Text>
          </Box>
        )}
        {messages.map((msg) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        <div ref={messagesEndRef} />
      </Box>

      <Divider />

      <Box sx={{ p: 1.5, display: "flex", gap: 1, alignItems: "flex-end", flexShrink: 0 }}>
        <TextField
          inputRef={inputRef}
          fullWidth
          size="small"
          multiline
          maxRows={4}
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={isStreaming}
          sx={{
            "& .MuiOutlinedInput-root": { borderRadius: 2, fontSize: 14 },
          }}
        />
        {isStreaming ? (
          <Tooltip title="Stop">
            <IconButton color="error" onClick={stopStreaming} size="small">
              <Square size={18} />
            </IconButton>
          </Tooltip>
        ) : (
          <Tooltip title="Send (Enter)">
            <span>
              <IconButton
                color="primary"
                onClick={handleSend}
                disabled={!input.trim()}
                size="small"
              >
                <Send size={18} />
              </IconButton>
            </span>
          </Tooltip>
        )}
      </Box>
    </Box>
  );
}
