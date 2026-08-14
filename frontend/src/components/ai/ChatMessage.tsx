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
import { memo, useMemo } from "react";
import Box from "@mui/material/Box";
import Avatar from "@mui/material/Avatar";
import { Text } from "@nvidia/foundations-react-core";
import { Bot, User } from "lucide-react";
import type { ChatMessage as ChatMessageType } from "../../context/AIChatContext";

function formatMarkdown(text: string): string {
  return text
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="lang-$1">$2</code></pre>')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/^(\d+)\. (.+)$/gm, "<li>$2</li>")
    .replace(/(<li>[\s\S]*?<\/li>)/g, "<ul>$1</ul>")
    .replace(/<\/ul>\s*<ul>/g, "")
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n/g, "<br/>");
}

interface ChatMessageProps {
  message: ChatMessageType;
}

function ChatMessageComponent({ message }: ChatMessageProps) {
  const isUser = message.role === "user";
  const displayHtml = useMemo(
    () => (isUser ? null : formatMarkdown(message.content)),
    [isUser, message.content],
  );

  return (
    <Box
      sx={{
        display: "flex",
        gap: 1.5,
        px: 2,
        py: 1,
        flexDirection: isUser ? "row-reverse" : "row",
        alignItems: "flex-start",
      }}
    >
      <Avatar
        sx={{
          width: 28,
          height: 28,
          bgcolor: isUser ? "primary.main" : "#76B900",
          flexShrink: 0,
          mt: 0.5,
        }}
      >
        {isUser ? <User size={16} /> : <Bot size={16} />}
      </Avatar>

      <Box
        sx={{
          maxWidth: "80%",
          bgcolor: isUser ? "primary.main" : "action.hover",
          color: isUser ? "primary.contrastText" : "text.primary",
          borderRadius: 2,
          px: 1.5,
          py: 1,
          "& p": { m: 0, fontSize: 14, lineHeight: 1.6 },
          "& p + p": { mt: 1 },
          "& pre": {
            bgcolor: "grey.900",
            color: "grey.100",
            borderRadius: 1,
            p: 1,
            my: 1,
            overflow: "auto",
            fontSize: 13,
          },
          "& code": { fontSize: 13, fontFamily: "monospace" },
          "& :not(pre) > code": {
            bgcolor: isUser ? "rgba(255,255,255,0.15)" : "action.selected",
            px: 0.5,
            borderRadius: 0.5,
          },
          "& ul, & ol": { pl: 2, my: 0.5, fontSize: 14 },
          "& li": { mb: 0.25 },
          "& strong": { fontWeight: 600 },
        }}
      >
        {isUser ? (
          <Text kind="body/regular/sm" style={{ whiteSpace: "pre-wrap" }}>
            {message.content}
          </Text>
        ) : (
          <>
            {displayHtml ? (
              <Box
                component="div"
                dangerouslySetInnerHTML={{ __html: displayHtml }}
                sx={{ "& > :first-of-type": { mt: 0 }, "& > :last-child": { mb: 0 } }}
              />
            ) : null}
            {message.streaming && !message.content && (
              <Box sx={{ display: "flex", gap: 0.5, py: 0.5 }}>
                {[0, 1, 2].map((i) => (
                  <Box
                    key={i}
                    sx={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      bgcolor: "text.secondary",
                      animation: "pulse 1.4s infinite",
                      animationDelay: `${i * 0.2}s`,
                      "@keyframes pulse": {
                        "0%, 80%, 100%": { opacity: 0.3 },
                        "40%": { opacity: 1 },
                      },
                    }}
                  />
                ))}
              </Box>
            )}
            {message.streaming && message.content && (
              <Box
                component="span"
                sx={{
                  display: "inline-block",
                  width: 6,
                  height: 14,
                  bgcolor: "text.primary",
                  ml: 0.25,
                  animation: "blink 0.8s infinite",
                  "@keyframes blink": {
                    "0%, 50%": { opacity: 1 },
                    "51%, 100%": { opacity: 0 },
                  },
                }}
              />
            )}
          </>
        )}
      </Box>
    </Box>
  );
}

export default memo(ChatMessageComponent);
