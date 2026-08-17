# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Agentic tool-use for the NVIS Rack Status assistant.

A single in-process tool registry (``tools.REGISTRY``) is the source of truth for the
callable actions the AI assistant (and external MCP clients) can invoke. Tool handlers
wrap the SAME internal functions the HTTP routes use, so agent behavior matches the UI
exactly, including the per-feature ``RACK_*_DISABLED`` gates.

Modules:
  * ``tools``          - ``AgentTool`` dataclass, ``REGISTRY``, schema + execution helpers.
  * ``registrations``  - wires the curated read/action/workflow tools to app internals.
  * ``loop``           - the LLM tool-calling loop (approval-gated writes, streamed events).
  * ``audit``          - append-only JSONL audit of every tool call.
  * ``mcp_server``     - thin MCP facade exposing the same registry to external agents.
"""
