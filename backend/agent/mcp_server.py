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
"""Thin MCP server facade exposing the SAME agent tool registry to external agents.

Runs as a separate stdio entrypoint (``python -m agent.mcp_server``) so Cursor/Claude and
other MCP clients can drive rack-status. It reuses :data:`agent.tools.REGISTRY` verbatim —
no duplicated action logic.

Approval policy differs by transport: external MCP callers have no in-chat approval UI, so
mutating tools are HIDDEN and REFUSED unless ``RACK_MCP_ALLOW_WRITES=1``. Read tools are
always available. The per-feature ``RACK_*_DISABLED`` gates still apply.

The pure helpers :func:`list_tools_payload` and :func:`call_tool` are transport-agnostic and
unit-testable without the MCP SDK installed; :func:`main` is the only part that imports it.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import registrations
from . import tools as agent_tools

_logger = logging.getLogger(__name__)
_TRUTHY = ("1", "true", "yes", "on")


def _allow_writes() -> bool:
    return os.environ.get("RACK_MCP_ALLOW_WRITES", "").strip().lower() in _TRUTHY


def mcp_enabled() -> bool:
    return os.environ.get("RACK_MCP_ENABLED", "").strip().lower() in _TRUTHY


def list_tools_payload() -> list[dict[str, Any]]:
    """MCP ``tools/list`` payload: available tools (writes hidden unless allowed)."""
    registrations.register_all()
    allow = _allow_writes()
    out: list[dict[str, Any]] = []
    for t in agent_tools.available_tools():
        if t.side_effect and not allow:
            continue
        out.append({
            "name": t.name,
            "description": t.description,
            "inputSchema": t.parameters or {"type": "object", "properties": {}},
        })
    return out


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """MCP ``tools/call``: refuse mutating tools unless RACK_MCP_ALLOW_WRITES=1."""
    registrations.register_all()
    tool = agent_tools.get_tool(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    allow = _allow_writes()
    if tool.side_effect and not allow:
        return {
            "ok": False,
            "error": f"tool {name!r} has side effects; set RACK_MCP_ALLOW_WRITES=1 to permit MCP writes",
        }
    return agent_tools.execute(name, arguments or {}, allow_side_effects=allow)


def main() -> None:  # pragma: no cover - requires the MCP SDK + a stdio client
    """Run the MCP server over stdio. Requires the ``mcp`` package (see requirements)."""
    import anyio
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    server = Server("nvis-rack-status")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [
            types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in list_tools_payload()
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict | None) -> list[types.TextContent]:
        result = call_tool(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    _logger.info("nvis-rack-status MCP server starting (writes allowed=%s)", _allow_writes())
    anyio.run(_run)


if __name__ == "__main__":  # pragma: no cover
    main()
