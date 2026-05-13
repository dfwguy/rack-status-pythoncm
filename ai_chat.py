"""AI Chat module — LLM client + Flask route helpers for the AI assistant.

Supports NVIDIA NIM, OpenAI-compatible endpoints, vLLM, Ollama, and local Llama.
Settings are persisted to var/cache/ai_settings.json.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
import uuid
from pathlib import Path
from typing import Generator

import requests

_logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
AI_SETTINGS_FILE = Path(
    os.environ.get("RACK_AI_SETTINGS_FILE", "")
    or str(APP_DIR / "var" / "cache" / "ai_settings.json")
)

_LLM_TIMEOUT_S = 180
_LLM_MAX_RETRIES = 3
_LLM_RETRY_DELAY_S = 3
_LLM_TEMPERATURE = 0.3
_LLM_MAX_TOKENS = 4096

_DEFAULT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
_DEFAULT_MODEL = "nvidia/llama-3.1-nemotron-ultra-253b-v1"

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
    with _settings_lock:
        settings = _load_settings()
    if settings.get("enabled") is False:
        return False
    return bool(settings.get("api_key") or os.environ.get("RACK_AI_API_KEY", "").strip())


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_BASE = """\
You are the NVIS Rack Status AI Assistant. You help users understand and troubleshoot \
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


def build_system_prompt(context: dict, custom_rules: str = "") -> str:
    parts = [_SYSTEM_PROMPT_BASE]

    page = context.get("page", "unknown")
    rack = context.get("rack")
    if page == "singleRack" and rack:
        parts.append(f"\nThe user is currently viewing rack: {rack}")
    elif page == "dashboard":
        parts.append("\nThe user is on the cluster dashboard overview.")
    elif page == "slurm":
        parts.append("\nThe user is viewing Slurm job/node status.")
    elif page == "analytics":
        parts.append("\nThe user is on the analytics/aggregate view.")
    elif page == "tools":
        parts.append("\nThe user is in the utility/tools section.")
    elif page == "workflows":
        parts.append("\nThe user is in the workflow designer.")

    if context.get("additional_context"):
        parts.append(f"\nAdditional context: {context['additional_context']}")

    if custom_rules:
        parts.append(f"\nAdditional rules:\n{custom_rules}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM streaming client (OpenAI-compatible)
# ---------------------------------------------------------------------------


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output."""
    import re
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
# Demo mode responses
# ---------------------------------------------------------------------------

_DEMO_RESPONSES = [
    "Based on the current rack status, I can see that all nodes are healthy with NVLink bandwidth within expected ranges (>40 GB/s per link).",
    "The validation results show NCCL all-reduce tests passed on all 8 GPUs. Bus bandwidth: 42.3 GB/s average.",
    "Looking at the Slurm state, 3 nodes are currently in 'drain' state. This is typically due to a failed health check. I'd recommend:\n\n1. Check `scontrol show node <nodename>` for the drain reason\n2. Run GPU diagnostics: `nvidia-smi -q`\n3. If hardware looks good, undrain with `scontrol update nodename=<node> state=idle`",
    "For troubleshooting InfiniBand connectivity issues on this rack, I suggest:\n\n- Check link status: `ibstat`\n- Verify port counters: `perfquery`\n- Check for errors: `ibdiagnet`\n- If a port is down, try `ibportstate` to reset it",
]
_demo_idx = 0


def demo_stream_response(messages: list[dict]) -> Generator[str, None, None]:
    """Return a canned response for demo mode, yielding word-by-word."""
    global _demo_idx
    response = _DEMO_RESPONSES[_demo_idx % len(_DEMO_RESPONSES)]
    _demo_idx += 1

    words = response.split(" ")
    for i, word in enumerate(words):
        yield word + (" " if i < len(words) - 1 else "")
        time.sleep(0.03)


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
