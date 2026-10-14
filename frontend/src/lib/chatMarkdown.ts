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
 * Assistant replies as sanitized HTML: ``marked`` parses the GitHub-flavoured Markdown the model
 * writes (tables, headings, rules, fenced code) and a private DOMPurify instance strips anything
 * executable before ChatMarkdown inserts it. Imported only by the lazily loaded ChatMarkdown, so
 * the parser stays out of the first-paint bundle.
 */
import DOMPurify from "dompurify";
import { Marked } from "marked";

// ``breaks``: a single newline is a line break, as the replaced formatter drew it and as the model
// writes short stacked lines. Tables, lists and fences parse the same either way.
const markdown = new Marked({ gfm: true, breaks: true });

// A private instance: the shared default is also mermaid's, and a hook added there would rewrite
// every diagram's links too.
const purifier = DOMPurify(window);
purifier.addHook("afterSanitizeAttributes", (node) => {
  // A link in a reply opens beside the app rather than navigating the operator away from it.
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

/** Sanitized HTML for an assistant reply. A reply cut off mid-stream must not throw. */
export function renderChatMarkdown(text: string): string {
  const html = markdown.parse(text, { async: false });
  return purifier.sanitize(html, { USE_PROFILES: { html: true } });
}
