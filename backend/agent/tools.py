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
"""Agent tool registry: the single source of truth for AI-callable actions.

Each :class:`AgentTool` declares a name, human/LLM description, a JSON-schema for its
parameters, a category (``read`` / ``action`` / ``workflow``), whether it has side effects
(``side_effect`` -> requires human approval in the chat loop), an optional ``disabled_env``
(the ``RACK_*_DISABLED`` flag that hides/blocks it), and a ``handler(args) -> dict``.

The registry is consumed three ways from ONE definition:
  * the in-process LLM tool-calling loop (:mod:`agent.loop`),
  * the MCP server facade (:mod:`agent.mcp_server`),
  * tool-availability reporting for the UI.

Handlers are wired in :mod:`agent.registrations` (which imports ``app`` lazily) so this
module has no dependency on the Flask app and stays cheap to import.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    category: str = "read"  # read | action | workflow
    side_effect: bool = False  # True -> mutating, requires approval in the chat loop
    disabled_env: str | None = None  # e.g. "RACK_SLURM_SCANCEL_DISABLED"
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def is_available(self) -> bool:
        """False when this tool's ``RACK_*_DISABLED`` flag is set (hard-gated off)."""
        if not self.disabled_env:
            return True
        return os.environ.get(self.disabled_env, "").strip().lower() not in _TRUTHY

    def to_openai_schema(self) -> dict[str, Any]:
        """OpenAI-compatible ``tools[]`` function schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "category": self.category,
            "side_effect": self.side_effect,
            "available": self.is_available(),
            "disabled_env": self.disabled_env,
        }


REGISTRY: dict[str, AgentTool] = {}


def register(tool: AgentTool) -> None:
    REGISTRY[tool.name] = tool


def get_tool(name: str) -> AgentTool | None:
    return REGISTRY.get(name)


def available_tools() -> list[AgentTool]:
    return [t for t in REGISTRY.values() if t.is_available()]


def openai_tool_schemas() -> list[dict[str, Any]]:
    """Function schemas for every currently-available tool (LLM native tool-calling)."""
    return [t.to_openai_schema() for t in available_tools()]


def tools_text_catalog() -> str:
    """Compact text list of tools for the JSON-fallback protocol / system prompt."""
    lines: list[str] = []
    for t in available_tools():
        tag = "ACTION (needs approval)" if t.side_effect else t.category
        props = ", ".join((t.parameters.get("properties") or {}).keys())
        lines.append(f"- {t.name}({props}) [{tag}]: {t.description}")
    return "\n".join(lines)


def execute(
    name: str,
    args: dict[str, Any] | None,
    *,
    allow_side_effects: bool = True,
) -> dict[str, Any]:
    """Run a tool by name. Returns a JSON-able dict; never raises.

    ``allow_side_effects=False`` refuses mutating tools (used by the MCP facade when
    ``RACK_MCP_ALLOW_WRITES`` is off — external callers have no approval UI).
    """
    tool = REGISTRY.get(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {name!r}"}
    if not tool.is_available():
        return {"ok": False, "error": f"tool {name!r} is disabled ({tool.disabled_env})"}
    if tool.side_effect and not allow_side_effects:
        return {"ok": False, "error": f"tool {name!r} has side effects and is not permitted here"}
    if tool.handler is None:  # pragma: no cover - registration bug
        return {"ok": False, "error": f"tool {name!r} has no handler"}
    try:
        result = tool.handler(dict(args or {}))
    except Exception as exc:  # handlers must never crash the loop
        _logger.exception("agent tool %s failed", name)
        return {"ok": False, "error": str(exc)}
    if not isinstance(result, dict):
        return {"ok": True, "result": result}
    return result
