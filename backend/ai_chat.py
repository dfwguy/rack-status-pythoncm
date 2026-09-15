# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""AI Chat module — LLM client + Flask route helpers for the AI assistant.

Supports NVIDIA NIM, OpenAI-compatible endpoints, vLLM, Ollama, and local Llama.
Settings are persisted to var/cache/ai_settings.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path

import requests
from json_db import atomic_write_json

_logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
# Runtime state lives under repo-root var/ (parent of backend/).
REPO_ROOT = APP_DIR.parent
AI_SETTINGS_FILE = Path(
    os.environ.get("RACK_AI_SETTINGS_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "ai_settings.json")
)

_LLM_TIMEOUT_S = 180
_LLM_MAX_RETRIES = 3
_LLM_RETRY_DELAY_S = 3
_LLM_TEMPERATURE = 0.3
_LLM_MAX_TOKENS = 4096

_DEFAULT_ENDPOINT = "https://inference-api.nvidia.com/v1/chat/completions"
_DEFAULT_MODEL = "nvidia/nvidia/nemotron-3-super-v3"

_settings_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------


def _load_settings() -> dict:
    try:
        with open(AI_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(data: dict) -> None:
    AI_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(AI_SETTINGS_FILE, data)


def get_ai_settings() -> dict:
    with _settings_lock:
        settings = _load_settings()
    env_key = os.environ.get("RACK_AI_API_KEY", "").strip()
    return {
        "enabled": settings.get("enabled", bool(env_key)),
        "endpoint": settings.get("endpoint", "")
        or os.environ.get("RACK_AI_ENDPOINT", "").strip()
        or _DEFAULT_ENDPOINT,
        "model": settings.get("model", "")
        or os.environ.get("RACK_AI_MODEL", "").strip()
        or _DEFAULT_MODEL,
        "api_key_configured": bool(settings.get("api_key") or env_key),
        "api_key_masked": _mask_key(settings.get("api_key", "") or env_key),
        "system_rules": settings.get("system_rules", ""),
    }


def update_ai_settings(updates: dict) -> dict:
    with _settings_lock:
        settings = _load_settings()
        if "enabled" in updates and isinstance(updates["enabled"], bool):
            settings["enabled"] = updates["enabled"]
        if "endpoint" in updates and isinstance(updates["endpoint"], str):
            settings["endpoint"] = updates["endpoint"].strip()
        if "model" in updates and isinstance(updates["model"], str):
            settings["model"] = updates["model"].strip()
        if "api_key" in updates and isinstance(updates["api_key"], str) and updates["api_key"].strip():
            settings["api_key"] = updates["api_key"].strip()
        if "system_rules" in updates and isinstance(updates["system_rules"], str):
            settings["system_rules"] = updates["system_rules"]
        settings["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_settings(settings)
    return get_ai_settings()


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return ""
    return f"{key[:4]}{'•' * max(0, len(key) - 8)}{key[-4:]}"


def _get_effective_config() -> dict:
    with _settings_lock:
        settings = _load_settings()
    env_key = os.environ.get("RACK_AI_API_KEY", "").strip()
    api_key = settings.get("api_key", "") or env_key
    if not api_key:
        raise ValueError("AI is not configured — no API key found. Set one in Settings or via RACK_AI_API_KEY.")
    return {
        "endpoint": settings.get("endpoint", "").strip()
        or os.environ.get("RACK_AI_ENDPOINT", "").strip()
        or _DEFAULT_ENDPOINT,
        "model": settings.get("model", "").strip()
        or os.environ.get("RACK_AI_MODEL", "").strip()
        or _DEFAULT_MODEL,
        "api_key": api_key,
    }


def is_ai_enabled() -> bool:
    """AI is enabled if an API key is configured (settings or env), regardless of the toggle."""
    with _settings_lock:
        settings = _load_settings()
    has_key = bool(settings.get("api_key") or os.environ.get("RACK_AI_API_KEY", "").strip())
    if not has_key:
        return False
    return not (settings.get("enabled") is False and not has_key)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_BASE = """\
You are the NVIS Greenlight AI Assistant. You help users understand and troubleshoot \
NVIDIA GPU cluster racks. You have expertise in:

- Rack validation status (NCCL tests, GPU health, NVLink, InfiniBand)
- Slurm job scheduling and node states (drain, idle, alloc, down)
- BCM (Base Command Manager) cluster management via cmsh
- Network topology and switch configuration
- NEMO framework validation runs
- GPU reset, power cycling, and hardware diagnostics
- Log analysis and error pattern recognition

Guidelines:
- Be concise and actionable. Prefer bullet points for multi-step answers.
- When suggesting commands, use the platform context (BCM vs MAAS).
- If the user is looking at a specific rack, focus advice on that rack.
- Reference validation results, NCCL bandwidth numbers, and Slurm states when relevant.
- For troubleshooting, suggest a systematic workflow: check status → isolate → fix → verify.
"""


def _deployment_envelope(context: dict) -> str:
    """Authoritative platform/cluster/customer block from the catalog.

    Always injected (independent of any frontend-supplied ``additional_context``) so
    the model never reasons with the wrong hardware generation. Resolved for the
    request's cluster when provided, else the active cluster.
    """
    try:
        from catalog import context as catalog_context

        cluster_id = (context.get("cluster") or context.get("cluster_id") or "").strip() or None
        return catalog_context.build_ai_context_block(cluster_id=cluster_id)
    except Exception:  # pragma: no cover - catalog optional
        _logger.debug("_deployment_envelope: ignoring error", exc_info=True)
        return ""


_live_inventory_provider: Callable[[], str] | None = None


def set_live_inventory_provider(fn: Callable[[], str] | None) -> None:
    """Register the callable that returns the live cluster-inventory block.

    A hook rather than an import: ``app`` imports this module, so reaching back for the
    rack-status snapshot it owns would be a cycle. Registering it means every caller of
    :func:`build_system_prompt` gets the deployed counts — the chat route, the agent loop and
    workflow generation alike — instead of each one having to remember to pass them.
    """
    global _live_inventory_provider
    _live_inventory_provider = fn


def _live_inventory(context: dict) -> str:
    """Live DEPLOYED cluster size (racks/nodes/GPUs), caller-supplied or from the provider."""
    explicit = (context.get("live_inventory") or "").strip()
    if explicit:
        return explicit
    if _live_inventory_provider is None:
        return ""
    try:
        return (_live_inventory_provider() or "").strip()
    except Exception:
        _logger.debug("_live_inventory: ignoring error", exc_info=True)
        return ""


def build_system_prompt(context: dict, custom_rules: str = "") -> str:
    parts = [_SYSTEM_PROMPT_BASE]

    page = context.get("page", "unknown")
    rack = context.get("rack")

    # Authoritative deployment context FIRST so platform/customer facts cannot be omitted
    # or contradicted by page context.
    envelope = _deployment_envelope(context)
    if envelope:
        parts.append(f"\n{envelope}")

    # The envelope above carries per-rack platform GEOMETRY (e.g. 72 GPUs/rack) and cannot know
    # how many racks are actually deployed. This block carries the real inventory, so cluster-size
    # questions are answered from the snapshot instead of guessed from the naming pattern.
    live_inventory = _live_inventory(context)
    if live_inventory:
        parts.append(f"\n{live_inventory}")

    # Page-aware context. The frontend normally pushes a rich, registry-derived
    # `additional_context`; when present it takes precedence. Otherwise fall back
    # to the same content server-side via the help-content registry, and finally
    # to a minimal rack hint.
    additional = (context.get("additional_context") or "").strip()
    if additional:
        parts.append(f"\nPage context:\n{additional}")
    else:
        registry_context = ""
        try:
            import help_content

            registry_context = help_content.get_ai_context(page, rack)
        except Exception:  # pragma: no cover - registry is optional
            _logger.debug("build_system_prompt: ignoring error", exc_info=True)
            registry_context = ""
        if registry_context:
            parts.append(f"\nPage context:\n{registry_context}")
        elif page == "singleRack" and rack:
            parts.append(f"\nThe user is currently viewing rack: {rack}")

    if custom_rules:
        parts.append(f"\nAdditional rules:\n{custom_rules}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM streaming client (OpenAI-compatible)
# ---------------------------------------------------------------------------


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output."""
    result = re.sub(r"<think>[\s\S]*?</think>\s*", "", text)
    close_idx = result.find("</think>")
    if close_idx != -1:
        result = result[close_idx + len("</think>"):]
    open_idx = result.find("<think>")
    if open_idx != -1:
        result = result[:open_idx]
    return result.strip()


def chat_completion_stream(
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Generator[str, None, None]:
    """Stream chat completion tokens from the configured LLM endpoint.

    Yields content strings as they arrive. Handles <think> block stripping.
    """
    config = _get_effective_config()
    temp = temperature if temperature is not None else _LLM_TEMPERATURE
    max_tok = max_tokens if max_tokens is not None else _LLM_MAX_TOKENS

    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tok,
        "stream": True,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
    }

    last_error = None
    for attempt in range(1, _LLM_MAX_RETRIES + 1):
        try:
            _logger.info(
                "AI stream attempt %d/%d → %s model=%s",
                attempt, _LLM_MAX_RETRIES, config["endpoint"], config["model"],
            )
            resp = requests.post(
                config["endpoint"],
                json=payload,
                headers=headers,
                stream=True,
                timeout=_LLM_TIMEOUT_S,
            )

            if resp.status_code != 200:
                error_text = resp.text[:500]
                raise RuntimeError(f"LLM API returned {resp.status_code}: {error_text}")

            full_text = ""
            think_close_idx = -1
            yielded_len = 0

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    if think_close_idx == -1 and full_text:
                        cleaned = _strip_think_blocks(full_text)
                        if cleaned:
                            yield cleaned
                    return

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                content = None
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")

                if not content:
                    continue

                full_text += content

                if think_close_idx == -1:
                    think_close_idx = full_text.find("</think>")
                    if think_close_idx == -1:
                        continue

                after_think = full_text[think_close_idx + len("</think>"):]
                visible = after_think.lstrip()
                if len(visible) > yielded_len:
                    yield visible[yielded_len:]
                    yielded_len = len(visible)

            if think_close_idx == -1 and full_text:
                cleaned = _strip_think_blocks(full_text)
                if cleaned:
                    yield cleaned
            return

        except (requests.RequestException, RuntimeError, OSError) as exc:
            last_error = exc
            is_retryable = (
                "timed out" in str(exc).lower()
                or "returned 5" in str(exc)
                or "returned 429" in str(exc)
            )
            if is_retryable and attempt < _LLM_MAX_RETRIES:
                delay = _LLM_RETRY_DELAY_S * attempt
                _logger.warning("AI attempt %d failed: %s. Retrying in %ds…", attempt, exc, delay)
                time.sleep(delay)
                continue
            raise

    if last_error:
        raise last_error


def chat_completion(
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Non-streaming chat completion. Returns the full response text."""
    config = _get_effective_config()
    temp = temperature if temperature is not None else _LLM_TEMPERATURE
    max_tok = max_tokens if max_tokens is not None else _LLM_MAX_TOKENS

    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tok,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
    }

    last_error = None
    for attempt in range(1, _LLM_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                config["endpoint"],
                json=payload,
                headers=headers,
                timeout=_LLM_TIMEOUT_S,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"LLM API returned {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices or not choices[0].get("message", {}).get("content"):
                raise RuntimeError(f"Invalid LLM response: {json.dumps(data)[:300]}")

            return _strip_think_blocks(choices[0]["message"]["content"])

        except (requests.RequestException, RuntimeError, OSError) as exc:
            last_error = exc
            is_retryable = (
                "timed out" in str(exc).lower()
                or "returned 5" in str(exc)
                or "returned 429" in str(exc)
            )
            if is_retryable and attempt < _LLM_MAX_RETRIES:
                time.sleep(_LLM_RETRY_DELAY_S * attempt)
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError("Unknown error in chat_completion")


# ---------------------------------------------------------------------------
# Tool calling (for the agentic loop in agent/loop.py)
# ---------------------------------------------------------------------------

# Cache of endpoint::model -> supports native OpenAI tools. Avoids re-failing a
# native call on every turn once we learn a model doesn't support it.
_TOOLS_CAP: dict[str, bool] = {}

_JSON_TOOL_INSTRUCTION = (
    "You can call tools. To call a tool, reply with ONLY a single JSON object on its own "
    'line: {"tool": "<name>", "args": { ... }}. Do not add prose around it. After you receive '
    "a [tool_result:...] message you may call another tool or give your final answer as normal "
    "prose (no JSON). Only call one tool at a time."
)


def _tools_mode() -> str:
    m = (os.environ.get("RACK_AI_TOOLS_MODE") or "auto").strip().lower()
    return m if m in ("auto", "native", "json", "off") else "auto"


def _post_chat(config: dict, payload: dict) -> requests.Response:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config['api_key']}"}
    return requests.post(config["endpoint"], json=payload, headers=headers, timeout=_LLM_TIMEOUT_S)


def _parse_json_tool_directive(text: str) -> dict | None:
    """Find the first ``{... "tool": ...}`` object in free text (JSON-fallback mode)."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("tool"):
            args = obj.get("args")
            return {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": str(obj["tool"]),
                "args": args if isinstance(args, dict) else {},
            }
    return None


def _native_tool_call(config: dict, messages: list[dict], tool_schemas: list[dict]) -> dict:
    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": _LLM_TEMPERATURE,
        "max_tokens": _LLM_MAX_TOKENS,
        "tools": tool_schemas,
        "tool_choice": "auto",
    }
    resp = _post_chat(config, payload)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM API returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    raw_calls = msg.get("tool_calls") or []
    tool_calls = []
    for tc in raw_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
            "name": fn.get("name") or "",
            "args": args if isinstance(args, dict) else {},
        })
    assistant_message: dict = {"role": "assistant", "content": msg.get("content") or ""}
    if raw_calls:
        assistant_message["tool_calls"] = raw_calls
    content = _strip_think_blocks(msg.get("content") or "") if msg.get("content") else ""
    return {
        "content": content,
        "tool_calls": tool_calls,
        "assistant_message": assistant_message,
        "mode": "native",
    }


def _json_tool_call(config: dict, messages: list[dict], tool_catalog_text: str) -> dict:
    aug_sys = {
        "role": "system",
        "content": f"{_JSON_TOOL_INSTRUCTION}\n\nAvailable tools:\n{tool_catalog_text}",
    }
    if messages and messages[0].get("role") == "system":
        aug = [messages[0], aug_sys, *messages[1:]]
    else:
        aug = [aug_sys, *messages]
    payload = {
        "model": config["model"],
        "messages": aug,
        "temperature": _LLM_TEMPERATURE,
        "max_tokens": _LLM_MAX_TOKENS,
    }
    resp = _post_chat(config, payload)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM API returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text = _strip_think_blocks(
        (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    )
    directive = _parse_json_tool_directive(text)
    if directive:
        return {
            "content": "",
            "tool_calls": [directive],
            "assistant_message": {"role": "assistant", "content": text},
            "mode": "json",
        }
    return {
        "content": text,
        "tool_calls": [],
        "assistant_message": {"role": "assistant", "content": text},
        "mode": "json",
    }


def chat_with_tools(messages: list[dict], tool_schemas: list[dict], tool_catalog_text: str) -> dict:
    """One tool-aware LLM turn (non-streaming).

    Returns ``{content, tool_calls[{id,name,args}], assistant_message, mode}``. Tries native
    OpenAI tool-calling; falls back to a constrained JSON protocol for models/endpoints that
    do not support ``tools`` (vLLM/Ollama variants, older NIM models).
    """
    config = _get_effective_config()
    mode = _tools_mode()
    if mode == "off":
        text = chat_completion(messages)
        return {
            "content": text,
            "tool_calls": [],
            "assistant_message": {"role": "assistant", "content": text},
            "mode": "off",
        }

    key = f"{config['endpoint']}::{config['model']}"
    if mode in ("auto", "native") and _TOOLS_CAP.get(key, True):
        try:
            return _native_tool_call(config, messages, tool_schemas)
        except RuntimeError as exc:
            if mode == "native":
                raise
            se = str(exc).lower()
            if any(tok in se for tok in ("400", "422", "tool", "function", "not support", "unsupported")):
                _TOOLS_CAP[key] = False  # remember: this model can't do native tools
            # fall through to JSON mode
    return _json_tool_call(config, messages, tool_catalog_text)


def tool_result_message(mode: str, call_id: str, name: str, result: dict) -> dict:
    """Build the message that feeds a tool result back to the model (mode-appropriate)."""
    payload = json.dumps(result, default=str)
    if len(payload) > 6000:
        payload = payload[:6000] + '…"}'
    if mode == "native":
        return {"role": "tool", "tool_call_id": call_id, "content": payload}
    return {"role": "user", "content": f"[tool_result:{name}] {payload}"}


# ---------------------------------------------------------------------------
# Demo mode — keyword-matched realistic responses
# ---------------------------------------------------------------------------

_DEMO_TOPIC_RESPONSES: list[tuple[list[str], str]] = [
    # Mermaid / workflow generation
    (
        ["mermaid", "graph td", "flowchart", "generate.*workflow", "generate.*diagram"],
        """\
graph TD
    start(["Start"]) -->|"begin"| check_nodes["Check Node Health"]
    check_nodes -->|"all healthy"| run_nccl["Run NCCL Test"]
    check_nodes -->|"nodes down"| check_slurm["Check Slurm State"]
    check_slurm --> undrain(["Undrain Nodes"])
    undrain --> run_nccl
    run_nccl -->|"pass"| done(["Done - Healthy"])
    run_nccl -->|"fail"| diagnose{"Hardware or Software?"}
    diagnose -->|"Hardware"| gpu_reset["Reset GPU"]
    diagnose -->|"Software"| restart_svc(["Restart Services"])
    gpu_reset -->|"recovered"| verify["Verify Fix"]
    gpu_reset -->|"still failing"| escalate(["Open HW Ticket"])
    restart_svc --> verify
    verify -->|"pass"| done
    verify -->|"fail"| escalate""",
    ),
    # Rack status
    (
        ["rack status", "rack health", "how.*rack", "status of", "what.*status", "overview"],
        ("Here is the current rack status summary:\n\n"
        "- **p34-r07**: All 18 nodes healthy, NCCL passed, bus BW 42.1 GB/s avg\n"
        "- **p34-r08**: 16/18 nodes healthy, 2 nodes in `drain` (gpu07, gpu12 — Xid 79 errors)\n"
        "- **p34-r09**: All 18 nodes healthy, NCCL passed, bus BW 41.8 GB/s avg\n"
        "- **p34-r10**: 17/18 nodes healthy, 1 node `down` (gpu03 — NVLink failure)\n\n"
        "Overall cluster health: **94.4%** (68/72 nodes operational). "
        "I'd recommend investigating the Xid 79 errors on p34-r08 first — those are typically recoverable with a GPU reset."),
    ),
    # NCCL
    (
        ["nccl", "all-reduce", "bandwidth", "bus bw", "collective"],
        ("The latest NCCL all-reduce test results across the cluster:\n\n"
        "| Rack | Nodes | Bus BW (avg) | Status |\n"
        "|------|-------|-------------|--------|\n"
        "| p34-r07 | 18/18 | 42.1 GB/s | PASS |\n"
        "| p34-r08 | 16/18 | 38.7 GB/s | PARTIAL — 2 nodes excluded |\n"
        "| p34-r09 | 18/18 | 41.8 GB/s | PASS |\n"
        "| p34-r10 | 17/18 | 40.2 GB/s | PASS — 1 node excluded |\n\n"
        "Expected bus bandwidth for H100 NVLink: **>40 GB/s** per link. "
        "p34-r08 is below threshold — the excluded nodes (gpu07, gpu12) are likely dragging the average when partially tested. "
        "Run `nccl-tests --rack p34-r08 --exclude gpu07,gpu12` to confirm the healthy nodes pass independently."),
    ),
    # Slurm / drain / nodes
    (
        ["slurm", "drain", "undrain", "sinfo", "squeue", "node state", "idle", "alloc", "scontrol"],
        ("Current Slurm node states:\n\n"
        "- **idle**: 42 nodes\n"
        "- **alloc**: 24 nodes (3 active jobs)\n"
        "- **drain**: 4 nodes — `gpu07@p34-r08` (Xid 79), `gpu12@p34-r08` (Xid 79), `gpu03@p34-r10` (NVLink), `gpu15@p34-r10` (health check timeout)\n"
        "- **down**: 2 nodes — `gpu03@p34-r10` (hardware), `mgmt01@p34-r11` (unreachable)\n\n"
        "To investigate a drained node:\n"
        "```\nscontrol show node gpu07-p34-r08\n```\n\n"
        "To undrain after fixing:\n"
        "```\nscontrol update nodename=gpu07-p34-r08 state=idle reason=\"cleared\"\n```\n\n"
        "The Xid 79 errors on p34-r08 nodes are GPU-level faults. Try `nvidia-smi -r` (GPU reset) first before hardware escalation."),
    ),
    # GPU issues
    (
        ["gpu", "nvidia-smi", "xid", "reset", "gpu reset", "gpu error", "gpu fail", "gpu health"],
        ("For GPU diagnostics, here's the recommended procedure:\n\n"
        "**1. Check GPU status:**\n"
        "```\nnvidia-smi -q --display=ERRORS\n```\n\n"
        "**2. Check for Xid errors in dmesg:**\n"
        "```\ndmesg | grep -i 'xid\\|nvrm\\|gpu'\n```\n\n"
        "Common Xid error codes:\n"
        "- **Xid 31**: GPU memory page fault — usually software, try restarting the job\n"
        "- **Xid 43**: GPU parity error — hardware issue, needs RMA\n"
        "- **Xid 48**: Double-bit ECC error — GPU reset may help, RMA if persistent\n"
        "- **Xid 79**: GPU fallen off the bus — try GPU reset, reseat if persists\n\n"
        "**3. Attempt GPU reset:**\n"
        "```\nnvidia-smi -r -i <gpu_id>\n```\n\n"
        "**4. If reset fails, power cycle the node:**\n"
        "```\nipmitool -H <bmc_ip> -U admin -P <pass> chassis power cycle\n```\n\n"
        "After recovery, run NCCL tests to validate the GPU is performing correctly."),
    ),
    # NVLink
    (
        ["nvlink", "nv link", "nvswitch", "link error", "link status", "topology"],
        ("NVLink diagnostics:\n\n"
        "**Check NVLink topology and status:**\n"
        "```\nnvidia-smi topo -m\nnvidia-smi nvlink -s\n```\n\n"
        "**Check for NVLink errors:**\n"
        "```\nnvidia-smi nvlink -e\n```\n\n"
        "Current cluster NVLink status:\n"
        "- p34-r07: All links active, 0 CRC errors\n"
        "- p34-r08: gpu07 — 3 NVLink links showing CRC errors (sub0, sub2, sub5)\n"
        "- p34-r09: All links active, 0 CRC errors\n"
        "- p34-r10: gpu03 — NVLink 2 down, hardware fault detected\n\n"
        "For gpu07@p34-r08, try clearing the counters with `nvidia-smi nvlink -r` and re-checking after 10 minutes. "
        "Persistent CRC errors indicate a cable or connector issue — reseat the NVLink bridge."),
    ),
    # InfiniBand / network
    (
        ["infiniband", "ib ", "ibstat", "ibdiag", "network", "switch", "rdma", "opa", "fabric"],
        ("InfiniBand fabric diagnostics:\n\n"
        "**1. Check local port status:**\n"
        "```\nibstat\nibstatus\n```\n\n"
        "**2. Check for errors and counters:**\n"
        "```\nperfquery\nibdiagnet --ls 10\n```\n\n"
        "**3. Check subnet manager:**\n"
        "```\nsminfo\nibswitches\n```\n\n"
        "Current fabric health:\n"
        "- Active ports: 284/288 (98.6%)\n"
        "- 4 ports down: 2 on leaf switch sw-p34-r08-l1 (likely related to drained nodes), 2 on spine switch sw-p34-spine01 (planned maintenance)\n"
        "- Symbol error rate: within threshold on all active ports\n\n"
        "If a node's IB port is down:\n"
        "```\nibportstate -D 0 1 reset\n```"),
    ),
    # NEMO / validation / training
    (
        ["nemo", "validation", "training", "llm", "megatron", "run validation", "test.*rack"],
        ("To run NEMO validation on a rack:\n\n"
        "**Quick validation (NCCL only, ~5 min):**\n"
        "```\n./bin/scripts/01-run-validation.sh --rack p34-r07 --test nccl\n```\n\n"
        "**Full validation (NCCL + NEMO training smoke test, ~30 min):**\n"
        "```\n./bin/scripts/01-run-validation.sh --rack p34-r07 --test full\n```\n\n"
        "**Latest validation results for the cluster:**\n"
        "- p34-r07: Full validation PASS (last run: 2h ago)\n"
        "- p34-r08: NCCL PARTIAL — 2 nodes excluded, re-validation needed after GPU reset\n"
        "- p34-r09: Full validation PASS (last run: 4h ago)\n"
        "- p34-r10: Not run since gpu03 went down\n\n"
        "I recommend re-running validation on p34-r08 and p34-r10 after addressing the drained/down nodes."),
    ),
    # Troubleshooting / help / how to
    (
        ["troubleshoot", "debug", "diagnos", "fix", "issue", "problem", "error", "fail", "wrong", "help"],
        ("Here's a systematic troubleshooting workflow:\n\n"
        "**Step 1 — Identify the issue:**\n"
        "- Check rack status on the Dashboard for an overview\n"
        "- Look for nodes in `drain` or `down` state in the Slurm view\n"
        "- Check if NCCL tests have failed recently\n\n"
        "**Step 2 — Isolate the root cause:**\n"
        "- GPU errors → Run `nvidia-smi -q`, check Xid codes in `dmesg`\n"
        "- NVLink issues → Run `nvidia-smi nvlink -s` and `-e` for errors\n"
        "- Network/IB → Run `ibstat`, `perfquery`, `ibdiagnet`\n"
        "- Software → Check if NEMO containers are mounted, Slurm config is correct\n\n"
        "**Step 3 — Apply fix:**\n"
        "- Xid errors → GPU reset (`nvidia-smi -r`)\n"
        "- NVLink down → Reseat cable, power cycle node\n"
        "- IB port down → `ibportstate reset`, check switch\n"
        "- Software → Restart services, re-pull containers\n\n"
        "**Step 4 — Verify:**\n"
        "- Run NCCL test on the affected rack\n"
        "- Confirm nodes are back to `idle` in Slurm\n"
        "- Run full NEMO validation if training jobs are planned\n\n"
        "Would you like me to walk through any of these steps in detail?"),
    ),
    # Power / IPMI / BMC
    (
        ["power", "ipmi", "bmc", "power cycle", "power on", "power off", "chassis"],
        ("For power management via IPMI/BMC:\n\n"
        "**Check power status:**\n"
        "```\nipmitool -H <bmc_ip> -U admin -P <pass> chassis status\n```\n\n"
        "**Power cycle a node:**\n"
        "```\nipmitool -H <bmc_ip> -U admin -P <pass> chassis power cycle\n```\n\n"
        "**Power on/off:**\n"
        "```\nipmitool -H <bmc_ip> -U admin -P <pass> chassis power on\nipmitool -H <bmc_ip> -U admin -P <pass> chassis power off\n```\n\n"
        "**Check sensor readings:**\n"
        "```\nipmitool -H <bmc_ip> -U admin -P <pass> sensor list\n```\n\n"
        "Note: Always drain the node in Slurm before power cycling to avoid disrupting running jobs. "
        "After power cycle, wait 2–3 minutes for POST, then verify with `ssh` and `nvidia-smi`."),
    ),
    # cmsh / BCM
    (
        ["cmsh", "bcm", "base command", "bright", "cluster manager", "cm "],
        ("Base Command Manager (BCM) / cmsh commands:\n\n"
        "**List racks:**\n"
        "```\ncmsh -c \"rack; list\"\n```\n\n"
        "**Check node status in a rack:**\n"
        "```\ncmsh -c \"device; status -n gpu*-p34-r07\"\n```\n\n"
        "**Run device setup:**\n"
        "```\ncmsh -c \"device; foreach -n gpu*-p34-r07 (setupnetwork)\"\n```\n\n"
        "**View hardware inventory:**\n"
        "```\ncmsh -c \"device; foreach -n gpu01-p34-r07 (get hardwareprofile)\"\n```\n\n"
        "Current BCM cluster status: 72 managed nodes, 4 in error state (matching the Slurm drain list)."),
    ),
]

_DEMO_FALLBACK = (
    "I can help you with rack status, GPU diagnostics, NCCL tests, Slurm management, "
    "NVLink troubleshooting, InfiniBand fabric health, NEMO validation, power management, "
    "and workflow design.\n\n"
    "Here are some things you can ask me:\n\n"
    "- \"What's the status of the racks?\"\n"
    "- \"How do I troubleshoot a GPU Xid error?\"\n"
    "- \"Show me the latest NCCL test results\"\n"
    "- \"Help me undrain nodes in Slurm\"\n"
    "- \"Check NVLink status on p34-r08\"\n"
    "- \"Generate a workflow for GPU troubleshooting\"\n\n"
    "What would you like to know?"
)


# Phrases that signal the user is asking about the current page/feature itself
# ("what can I do here", "what info is on this dashboard", "how do I use this").
_HELP_INTENT_PATTERNS = [
    r"this (page|dashboard|view|screen|section|tab|tool)",
    r"\bhere\b",
    r"what (information|info|data|can|could|else|features|options|actions)",
    r"how (do i|to|can i) use",
    r"available (in|on|here|to)",
    r"what('| i)s (here|on this|available|shown)",
    r"what does this",
    r"explain (this|the page|the dashboard)",
    r"tell me about this",
    r"what should i",
]


def _help_answer_for_page(context: dict | None) -> str | None:
    """Build a page-aware help answer from the knowledge registry, or None."""
    if not context:
        return None
    page = context.get("page")
    rack = context.get("rack")

    entry = None
    try:
        import help_content

        if page == "singleRack" and rack:
            entry = help_content.get_entry("rackDetail")
        if entry is None and page:
            entry = help_content.get_entry(page)
    except Exception:  # pragma: no cover - registry is optional
        _logger.debug("_help_answer_for_page: ignoring error", exc_info=True)
        entry = None

    if not entry:
        return None

    lines: list[str] = [f"## {entry.get('title', 'This page')}", "", entry.get("description", "")]

    features = entry.get("features") or []
    if features:
        lines.append("")
        lines.append("**What you can do here:**")
        lines.extend(f"- {f}" for f in features)

    howto = entry.get("howTo") or []
    if howto:
        lines.append("")
        lines.append("**Common tasks:**")
        for h in howto[:4]:
            steps = " → ".join(h.get("steps", []))
            lines.append(f"- *{h.get('task')}*: {steps}")

    faqs = entry.get("faqs") or []
    if faqs:
        lines.append("")
        lines.append("**FAQ:**")
        for f in faqs[:3]:
            lines.append(f"- **{f.get('q')}** {f.get('a')}")

    if rack:
        lines.append("")
        lines.append(f"You're currently focused on rack **{rack}**.")

    lines.append("")
    lines.append(
        "Open the **Help** page from the sidebar for the full guide, or ask me about a specific feature."
    )
    return "\n".join(lines)


def _match_demo_response(messages: list[dict], context: dict | None = None) -> str:
    """Find the best matching demo response based on the user's last message."""
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = (m.get("content") or "").lower()
            break
    if not user_text:
        return _help_answer_for_page(context) or _DEMO_FALLBACK

    # Page-aware help takes priority when the user is asking about the current
    # page/feature itself, so demo mode showcases the knowledge layer.
    if any(re.search(p, user_text) for p in _HELP_INTENT_PATTERNS):
        page_answer = _help_answer_for_page(context)
        if page_answer:
            return page_answer

    for keywords, response in _DEMO_TOPIC_RESPONSES:
        for kw in keywords:
            if re.search(kw, user_text):
                return response

    # No specific topic matched — prefer page context over the generic fallback.
    return _help_answer_for_page(context) or _DEMO_FALLBACK


def demo_stream_response(
    messages: list[dict], context: dict | None = None
) -> Generator[str, None, None]:
    """Return a realistic context-aware response for demo mode, streamed word-by-word."""
    response = _match_demo_response(messages, context)

    words = response.split(" ")
    for i, word in enumerate(words):
        yield word + (" " if i < len(words) - 1 else "")
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Test connection helper
# ---------------------------------------------------------------------------


def test_connection() -> dict:
    """Quick test to verify the configured AI endpoint is reachable."""
    try:
        config = _get_effective_config()
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    try:
        resp = requests.post(
            config["endpoint"],
            json={
                "model": config["model"],
                "messages": [{"role": "user", "content": "Say hello in one word."}],
                "max_tokens": 10,
                "temperature": 0,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config['api_key']}",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            return {"ok": True, "model": config["model"], "endpoint": config["endpoint"]}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}
