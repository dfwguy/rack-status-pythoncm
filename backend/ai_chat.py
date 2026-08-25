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
from collections.abc import Generator
from pathlib import Path

import requests

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
    tmp = AI_SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(AI_SETTINGS_FILE)


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
You are the NVIS Rack Status AI Assistant. You help users understand and troubleshoot \
NVIDIA GPU cluster racks. You have expertise in:

- Rack validation status (NCCL tests, GPU health, NVLink, InfiniBand)
- BCM 11 PythonCM ProgramRunner jobs and PythonCM node states (drain, idle, alloc, down)
- BCM cluster inventory and lifecycle management through PythonCM
- Network topology and switch configuration
- NEMO framework validation runs
- GPU reset, power cycling, and hardware diagnostics
- Log analysis and error pattern recognition

Guidelines:
- Be concise and actionable. Prefer bullet points for multi-step answers.
- This deployment is Nscale BCM 11. Prefer PythonCM for every equivalent operation, especially recurring status/inventory calls.
- Slurm is an allowed on-demand fallback only for features whose allocation, MPI, Pyxis, or LLMB semantics have no equivalent PythonCM implementation. State that fallback explicitly.
- Do not suggest cmsh polling. Do not imply that a preserved Slurm fallback runs on the 30-second Racks refresh path.
- If the user is looking at a specific rack, focus advice on that rack.
- Reference validation results, NCCL bandwidth numbers, and PythonCM states when relevant.
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


def build_system_prompt(context: dict, custom_rules: str = "") -> str:
    parts = [_SYSTEM_PROMPT_BASE]

    page = context.get("page", "unknown")
    rack = context.get("rack")

    # Authoritative deployment context FIRST so platform/customer facts cannot be omitted
    # or contradicted by page context.
    envelope = _deployment_envelope(context)
    if envelope:
        parts.append(f"\n{envelope}")

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
    check_nodes -->|"nodes down"| check_pythoncm["Check PythonCM State"]
    check_pythoncm --> undrain(["Resume Nodes with PythonCM"])
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
        ("Use the Racks view for the current Nscale TX snapshot. It refreshes every 30 seconds "
        "from PythonCM PhysicalNode, device-status, and node-state data. Demo chat does not "
        "invent rack names, health counts, or validation results."),
    ),
    # NCCL
    (
        ["nccl", "all-reduce", "bandwidth", "bus bw", "collective"],
        ("NCCL remains available through the documented on-demand Slurm fallback because the "
        "validated recipe needs allocation and MPI launcher semantics that PythonCM ProgramRunner "
        "does not reproduce. It is never called by the 30-second Racks refresh."),
    ),
    # PythonCM / drain / nodes. Legacy terms remain query synonyms only.
    (
        ["slurm", "drain", "undrain", "sinfo", "squeue", "node state", "idle", "alloc", "scontrol"],
        ("Use the Scheduler page for current Nscale PythonCM command and node state. "
        "Drain and Resume call PythonCM directly; ProgramRunner commands are identified by UUID. "
        "Demo chat does not fabricate live counts or node names."),
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
        "**4. If reset fails:** use the rack Power action, which calls PythonCM's device API.\n\n"
        "After recovery, run the PythonCM hardware and NVLink checks. Use the explicit Slurm "
        "fallback when the exact NCCL validation recipe is required."),
    ),
    # NVLink
    (
        ["nvlink", "nv link", "nvswitch", "link error", "link status", "topology"],
        ("NVLink diagnostics:\n\n"
        "**Check NVLink topology and status:**\n"
        "```\nnvidia-smi topo -m\nnvidia-smi nvlink -s\n```\n\n"
        "**Check for NVLink errors:**\n"
        "```\nnvidia-smi nvlink -e\n```\n\n"
        "Use the Nscale rack NVLink preflight for current node results. Demo chat has no live "
        "cluster telemetry and will not invent rack, GPU, link, or error-count state."),
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
        "Use the Nscale fabric probe for current read-only PythonCM ProgramRunner results. "
        "Switch resets and vendor-script audits remain available as explicit on-demand fallbacks "
        "where PythonCM has no equivalent. Demo chat does not invent fabric health."),
    ),
    # NEMO / validation / training
    (
        ["nemo", "validation", "training", "llm", "megatron", "run validation", "test.*rack"],
        ("Validation remains fully available. Rack Status uses PythonCM when an exact equivalent "
        "exists; the installed NCCL, NeMo, Llama, ClusterKit, NVLoom, MNUberGEMM, GPT-OSS, and "
        "system-info recipes use explicit on-demand Slurm or transport fallbacks where their "
        "allocation, MPI, Pyxis, LLMB, or file-transfer semantics have no PythonCM equivalent."),
    ),
    # Troubleshooting / help / how to
    (
        ["troubleshoot", "debug", "diagnos", "fix", "issue", "problem", "error", "fail", "wrong", "help"],
        ("Here's a systematic troubleshooting workflow:\n\n"
        "**Step 1 — Identify the issue:**\n"
        "- Check rack status on the Dashboard for an overview\n"
        "- Look for nodes in `drain` or `down` state in the Scheduler's PythonCM view\n"
        "- Review existing validation results and the provider shown for each launch\n\n"
        "**Step 2 — Isolate the root cause:**\n"
        "- GPU errors → Run `nvidia-smi -q`, check Xid codes in `dmesg`\n"
        "- NVLink issues → Run `nvidia-smi nvlink -s` and `-e` for errors\n"
        "- Network/IB → Run `ibstat`, `perfquery`, `ibdiagnet`\n"
        "- Software → Check container mounts and the PythonCM ProgramRunner result/log\n\n"
        "**Step 3 — Apply fix:**\n"
        "- Xid errors → GPU reset (`nvidia-smi -r`)\n"
        "- NVLink down → Reseat cable, power cycle node\n"
        "- IB port down → `ibportstate reset`, check switch\n"
        "- Software → Restart services, re-pull containers\n\n"
        "**Step 4 — Verify:**\n"
        "- Re-run the enabled PythonCM hardware/NVLink read-only checks\n"
        "- Confirm nodes are back to `idle` in PythonCM\n"
        "- Run NCCL or NeMo through the documented on-demand Slurm fallback when needed\n\n"
        "Would you like me to walk through any of these steps in detail?"),
    ),
    # Power / IPMI / BMC
    (
        ["power", "ipmi", "bmc", "power cycle", "power on", "power off", "chassis"],
        ("Use the Nscale rack Power action for status, on, off, or reset. It calls the "
        "PythonCM parallel device API and never shells out to IPMI or cmsh. Drain through "
        "PythonCM first and perform mutations only on an approved reserved node."),
    ),
    # PythonCM / BCM. Legacy terms remain query synonyms only.
    (
        ["cmsh", "bcm", "base command", "bright", "cluster manager", "cm "],
        ("This Nscale deployment uses BCM 11 with PythonCM as the primary provider.\n\n"
        "- Rack and node inventory comes from the PythonCM rack/device APIs.\n"
        "- Drain/resume and power actions use PythonCM device operations.\n"
        "- Equivalent commands use PythonCM ProgramRunner with UUID tracking.\n"
        "- Validations and other semantic gaps use explicit, on-demand Slurm/transport fallbacks.\n"
        "- The Racks view refreshes this PythonCM-derived state every 30 seconds.\n\n"
        "Use the Racks or Scheduler page for live state; this assistant does not fabricate a cluster snapshot."),
    ),
]

_DEMO_FALLBACK = (
    "I can help you with rack status, GPU diagnostics, NCCL tests, PythonCM job and node management, "
    "NVLink troubleshooting, InfiniBand fabric health, NEMO validation, power management, "
    "and workflow design.\n\n"
    "Here are some things you can ask me:\n\n"
    "- \"What's the status of the racks?\"\n"
    "- \"How do I troubleshoot a GPU Xid error?\"\n"
    "- \"Show me the latest NCCL test results\"\n"
    "- \"Help me resume nodes through PythonCM\"\n"
    "- \"Check NVLink status on an Nscale rack\"\n"
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
