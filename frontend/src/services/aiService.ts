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
import { apiFetch } from "../lib/apiFetch";

export interface ChatMessagePayload {
  role: "user" | "assistant";
  content: string;
}

export interface PageContext {
  page: string;
  rack?: string;
  additional_context?: string;
}

export interface AISettings {
  enabled: boolean;
  endpoint: string;
  model: string;
  api_key_configured: boolean;
  api_key_masked: string;
  system_rules: string;
}

/** A tool-loop event emitted by the agentic backend over SSE. */
export interface ToolEvent {
  type: "tool_call" | "tool_result" | "approval_required";
  tool: string;
  category?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  summary?: string;
  run_id?: string;
  call_id?: string;
}

export interface StreamHandlers {
  onChunk: (content: string) => void;
  onEvent?: (ev: ToolEvent) => void;
  onDone: () => void;
  onError: (error: string) => void;
}

const TOOL_EVENT_TYPES = new Set(["tool_call", "tool_result", "approval_required"]);

async function consumeSSE(response: Response, handlers: StreamHandlers): Promise<void> {
  if (!response.ok) {
    const text = await response.text();
    try {
      const json = JSON.parse(text);
      handlers.onError(json.error || `HTTP ${response.status}`);
    } catch {
      handlers.onError(text || `HTTP ${response.status}`);
    }
    return;
  }
  const reader = response.body?.getReader();
  if (!reader) {
    handlers.onError("No response body");
    return;
  }
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || !trimmed.startsWith("data: ")) continue;
      const data = trimmed.slice(6);
      if (data === "[DONE]") {
        handlers.onDone();
        return;
      }
      try {
        const parsed = JSON.parse(data);
        if (parsed.error) {
          handlers.onError(parsed.error);
          return;
        }
        if (typeof parsed.type === "string" && TOOL_EVENT_TYPES.has(parsed.type)) {
          handlers.onEvent?.(parsed as ToolEvent);
        } else if (parsed.content) {
          handlers.onChunk(parsed.content);
        }
      } catch {
        // skip malformed chunks
      }
    }
  }
  handlers.onDone();
}

export function streamChat(
  messages: ChatMessagePayload[],
  context: PageContext,
  handlers: StreamHandlers,
): AbortController {
  const controller = new AbortController();
  apiFetch("/api/ai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, context }),
    signal: controller.signal,
  })
    .then((response) => consumeSSE(response, handlers))
    .catch((err) => {
      if (err.name !== "AbortError") handlers.onError(err.message || "Stream failed");
    });
  return controller;
}

export interface ResumeRequest {
  run_id: string;
  call_id: string;
  decision: "approve" | "deny";
  edited_args?: Record<string, unknown>;
}

/** Resume a paused agent turn after the user approves/denies a mutating action. */
export function resumeChat(req: ResumeRequest, handlers: StreamHandlers): AbortController {
  const controller = new AbortController();
  apiFetch("/api/ai/chat/resume", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal: controller.signal,
  })
    .then((response) => consumeSSE(response, handlers))
    .catch((err) => {
      if (err.name !== "AbortError") handlers.onError(err.message || "Stream failed");
    });
  return controller;
}

export async function getAISettings(): Promise<AISettings> {
  const res = await apiFetch("/api/ai/settings");
  const json = await res.json();
  return json as AISettings;
}

export async function updateAISettings(
  settings: Partial<AISettings> & { api_key?: string },
): Promise<AISettings> {
  const res = await apiFetch("/api/ai/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  const json = await res.json();
  return json as AISettings;
}

export async function testAIConnection(): Promise<{ ok: boolean; error?: string; model?: string }> {
  const res = await apiFetch("/api/ai/test", { method: "POST" });
  return res.json();
}
