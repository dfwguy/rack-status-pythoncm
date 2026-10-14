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
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderChatMarkdown } from "../../lib/chatMarkdown";
import ChatMarkdown from "./ChatMarkdown";
import ChatMessage from "./ChatMessage";

// The shape of the Deployment Tracker answer that showed the problem: a heading, a rule, and a
// table whose rows the old formatter drew as one paragraph of pipes.
const REPLY = [
  "### 1. Page Layout & Key Sections",
  "",
  "| Section | What it shows | How to use it |",
  "|---|---|---:|",
  "| **Headline tiles** | Node counts per state | 3 |",
  "| Node heatmap | One tile per node | 12 |",
  "",
  "---",
  "",
  "Run `scontrol show node` then:",
  "",
  "```bash",
  "sinfo -N -o '%N %T'",
  "```",
].join("\n");

describe("renderChatMarkdown", () => {
  it("draws a GFM table as a table, with its header, rows and column alignment", () => {
    render(<ChatMarkdown text={REPLY} />);
    const table = screen.getByRole("table");
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Section", "What it shows", "How to use it"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    // Inline Markdown inside a cell still renders.
    expect(within(table).getByText("Headline tiles").tagName).toBe("STRONG");
    // `|---:|` right-aligns the column.
    expect(within(table).getByText("12")).toHaveAttribute("align", "right");
  });

  it("renders headings, rules and fenced code instead of printing their markers", () => {
    render(<ChatMarkdown text={REPLY} />);
    expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent("1. Page Layout & Key Sections");
    expect(screen.getByRole("separator")).toBeInTheDocument();
    const box = screen.getByTestId("chat-markdown");
    expect(box.textContent).not.toContain("###");
    expect(box.textContent).not.toContain("|---");
    expect(box.querySelector("pre > code")).toHaveTextContent("sinfo -N -o '%N %T'");
    expect(box.querySelector(":not(pre) > code")).toHaveTextContent("scontrol show node");
  });

  it("strips script and event handlers a reply could carry from a log line or a tool result", () => {
    const html = renderChatMarkdown(
      'node gpu1 says <img src=x onerror="alert(1)"> and <script>alert(2)</script> [x](javascript:alert(3))',
    );
    expect(html).not.toMatch(/onerror|<script|javascript:/i);
  });

  it("opens a reply's links beside the app", () => {
    const html = renderChatMarkdown("See [the runbook](https://docs.example/runbook).");
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("does not throw on a reply cut off mid-stream", () => {
    for (let i = 0; i <= REPLY.length; i += 7) expect(() => renderChatMarkdown(REPLY.slice(0, i))).not.toThrow();
  });
});

describe("ChatMessage", () => {
  it("renders an assistant reply as Markdown once the renderer has loaded", async () => {
    render(<ChatMessage message={{ id: "m1", role: "assistant", content: REPLY, timestamp: new Date(0) }} />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("keeps a user's own message as the text they typed", () => {
    render(<ChatMessage message={{ id: "m2", role: "user", content: "| a | b |\n|---|---|\n| 1 | 2 |", timestamp: new Date(0) }} />);
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText(/\| a \| b \|/)).toBeInTheDocument();
  });
});
