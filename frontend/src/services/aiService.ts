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

export async function streamChat(
  messages: ChatMessagePayload[],
  context: PageContext,
  onChunk: (content: string) => void,
  onDone: () => void,
  onError: (error: string) => void,
): Promise<AbortController> {
  const controller = new AbortController();

  apiFetch("/api/ai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, context }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        const text = await response.text();
        try {
          const json = JSON.parse(text);
          onError(json.error || `HTTP ${response.status}`);
        } catch {
          onError(text || `HTTP ${response.status}`);
        }
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        onError("No response body");
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
            onDone();
            return;
          }

          try {
            const parsed = JSON.parse(data);
            if (parsed.error) {
              onError(parsed.error);
              return;
            }
            if (parsed.content) {
              onChunk(parsed.content);
            }
          } catch {
            // skip malformed chunks
          }
        }
      }

      onDone();
    })
    .catch((err) => {
      if (err.name !== "AbortError") {
        onError(err.message || "Stream failed");
      }
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
