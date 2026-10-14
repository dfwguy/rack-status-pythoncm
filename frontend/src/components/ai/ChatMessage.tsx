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
import { lazy, memo, Suspense } from "react";
import Box from "@mui/material/Box";
import Avatar from "@mui/material/Avatar";
import { Text, Button, Banner } from "@nvidia/foundations-react-core";
import { Bot, User, Wrench, CheckCircle2, XCircle, Loader, ShieldAlert } from "lucide-react";
import {
  useOptionalAIChat,
  type ChatMessage as ChatMessageType,
  type ToolActivity,
  type PendingApproval,
} from "../../context/AIChatContext";

// The Markdown parser is only needed once a reply is drawn, and the widget mounts with the app.
const ChatMarkdown = lazy(() => import("./ChatMarkdown"));

function ToolChip({ activity }: { activity: ToolActivity }) {
  const running = activity.status === "running";
  const failed = activity.status === "done" && activity.ok === false;
  const Icon = running ? Loader : failed ? XCircle : CheckCircle2;
  const color = running
    ? "var(--text-color-secondary)"
    : failed
      ? "var(--text-color-feedback-danger)"
      : "var(--text-color-feedback-success)";
  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 0.75,
        px: 1,
        py: 0.5,
        my: 0.25,
        borderRadius: 1,
        bgcolor: "action.selected",
      }}
    >
      <Wrench size={14} />
      <Text kind="mono/sm" style={{ fontWeight: 600 }}>{activity.tool}</Text>
      <Icon size={14} color={color} style={running ? { animation: "spin 1s linear infinite" } : undefined} />
      {activity.summary ? (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {activity.summary}
        </Text>
      ) : null}
      <style>{"@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}"}</style>
    </Box>
  );
}

/**
 * The gate on every mutating action the agent proposes. Nothing runs until the operator
 * presses Approve, so the tool name and its arguments are spelled out verbatim rather than
 * summarized -- this is the last point at which a wrong rack can be caught.
 */
function ApprovalCard({ messageId, approval }: { messageId: string; approval: PendingApproval }) {
  const chat = useOptionalAIChat();
  const argList = Object.entries(approval.args || {})
    .filter(([k]) => !k.startsWith("_"))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(", ");
  return (
    <Box sx={{ my: 1 }}>
      <Banner status="warning" kind="inline">
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.75, mb: 0.5 }}>
          <ShieldAlert size={15} />
          <Text kind="body/bold/sm">Approve action</Text>
        </Box>
        <Text kind="mono/sm" style={{ display: "block", wordBreak: "break-all" }}>
          {approval.tool}({argList})
        </Text>
        <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
          <Button
            size="small"
            kind="primary"
            color="danger"
            onClick={() => chat?.resolveApproval(messageId, "approve")}
          >
            Approve &amp; run
          </Button>
          <Button
            size="small"
            kind="secondary"
            color="neutral"
            onClick={() => chat?.resolveApproval(messageId, "deny")}
          >
            Deny
          </Button>
        </Box>
      </Banner>
    </Box>
  );
}

interface ChatMessageProps {
  message: ChatMessageType;
}

function ChatMessageComponent({ message }: ChatMessageProps) {
  const isUser = message.role === "user";

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
          // Room for a table to scroll inside the bubble instead of pushing it wider.
          minWidth: 0,
        }}
      >
        {isUser ? (
          <Text kind="body/regular/sm" style={{ whiteSpace: "pre-wrap" }}>
            {message.content}
          </Text>
        ) : (
          <>
            {message.tools && message.tools.length > 0 && (
              <Box sx={{ mb: message.content ? 1 : 0.5 }}>
                {message.tools.map((t, i) => (
                  <ToolChip key={i} activity={t} />
                ))}
              </Box>
            )}
            {message.content ? (
              <Suspense
                fallback={
                  <Text kind="body/regular/sm" style={{ whiteSpace: "pre-wrap" }}>
                    {message.content}
                  </Text>
                }
              >
                <ChatMarkdown text={message.content} />
              </Suspense>
            ) : null}
            {message.approval && <ApprovalCard messageId={message.id} approval={message.approval} />}
            {message.streaming && !message.content && !(message.tools && message.tools.length) && (
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
