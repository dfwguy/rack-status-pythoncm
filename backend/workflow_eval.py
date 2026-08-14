# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Value-based evaluation for workflow steps.

Turns a step's raw result (exit code + stdout/stderr) into a pass/fail decision
plus named extracted values, driven by the declarative ``evaluation`` block:

    evaluation:
      success_when:            # optional; default = exit_code 0
        exit_code: 0
        stdout_contains: "Out of bounds values : 0 OK"
        stdout_not_contains: "error"
        stdout_regex: "busbw"
        stderr_contains: "..."
      extract:                 # optional; capture named values (first group)
        - {name: busbw_1g, regex: "1073741824 .*? ([0-9.]+)\\s+[0-9]+\\s*$", type: float}
      assert:                  # optional; numeric comparisons on extracted values
        - "busbw_1g >= 300"

Extracted values are returned so the engine can persist them on the run and make
them available to later steps. No code is ``eval``'d -- assertions are parsed.
"""
from __future__ import annotations

import re

_ASSERT_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(>=|<=|==|!=|>|<)\s*(-?[0-9.]+)\s*$")


def _cast(value: str, typ: str):
    typ = (typ or "str").lower()
    try:
        if typ == "int":
            return int(float(value))
        if typ == "float":
            return float(value)
    except (TypeError, ValueError):
        return value
    return value


def _run_extract(evaluation: dict, stdout: str, stderr: str) -> tuple[dict, list[str]]:
    outputs: dict = {}
    detail: list[str] = []
    for e in evaluation.get("extract") or []:
        if not isinstance(e, dict):
            continue
        name, rx = e.get("name"), e.get("regex")
        if not name or not rx:
            continue
        src = stderr if e.get("source") == "stderr" else stdout
        try:
            m = re.search(rx, src, re.MULTILINE)
        except re.error as exc:
            detail.append(f"extract {name}: bad regex ({exc})")
            continue
        if m:
            raw = m.group(1) if m.groups() else m.group(0)
            outputs[name] = _cast(raw, e.get("type", "str"))
            detail.append(f"extract {name}={outputs[name]}")
        else:
            detail.append(f"extract {name}: no match")
    return outputs, detail


def _check_success_when(sw: dict, returncode: int, stdout: str, stderr: str) -> tuple[bool, list[str]]:
    ok = True
    detail: list[str] = []
    if "exit_code" in sw:
        want = int(sw["exit_code"])
        cond = returncode == want
        ok = ok and cond
        detail.append(f"exit_code=={want}: {'ok' if cond else f'FAIL (got {returncode})'}")
    if "stdout_empty" in sw:
        want_empty = bool(sw["stdout_empty"])
        is_empty = stdout.strip() == ""
        cond = is_empty if want_empty else (not is_empty)
        ok = ok and cond
        detail.append(f"stdout_empty=={want_empty}: {'ok' if cond else 'FAIL'}")
    if "stdout_contains" in sw:
        cond = str(sw["stdout_contains"]) in stdout
        ok = ok and cond
        detail.append(f"stdout_contains: {'ok' if cond else 'FAIL'}")
    if "stdout_not_contains" in sw:
        cond = str(sw["stdout_not_contains"]) not in stdout
        ok = ok and cond
        detail.append(f"stdout_not_contains: {'ok' if cond else 'FAIL'}")
    if "stdout_regex" in sw:
        try:
            cond = bool(re.search(sw["stdout_regex"], stdout, re.MULTILINE))
        except re.error:
            cond = False
        ok = ok and cond
        detail.append(f"stdout_regex: {'ok' if cond else 'FAIL'}")
    if "stderr_contains" in sw:
        cond = str(sw["stderr_contains"]) in stderr
        ok = ok and cond
        detail.append(f"stderr_contains: {'ok' if cond else 'FAIL'}")
    return ok, detail


def _eval_assert(expr: str, outputs: dict) -> tuple[bool, str]:
    m = _ASSERT_RE.match(expr or "")
    if not m:
        return False, "unparseable assertion"
    name, op, num = m.group(1), m.group(2), float(m.group(3))
    if name not in outputs:
        return False, f"{name} not extracted"
    try:
        lhs = float(outputs[name])
    except (TypeError, ValueError):
        return False, f"{name}={outputs[name]!r} not numeric"
    ok = {
        ">=": lhs >= num, "<=": lhs <= num, ">": lhs > num,
        "<": lhs < num, "==": lhs == num, "!=": lhs != num,
    }[op]
    return ok, f"({lhs} {op} {num})"


def evaluate(
    evaluation: dict | None,
    *,
    returncode: int,
    stdout: str,
    stderr: str = "",
    outputs: dict | None = None,
) -> tuple[bool, dict, list[str]]:
    """Return ``(success, merged_outputs, detail_lines)``.

    With no ``evaluation`` block, success is ``returncode == 0`` (today's behavior).
    """
    merged = dict(outputs or {})
    if not evaluation or not isinstance(evaluation, dict):
        return returncode == 0, merged, []

    extracted, detail = _run_extract(evaluation, stdout, stderr)
    merged.update(extracted)

    sw = evaluation.get("success_when")
    if sw is None:
        ok = returncode == 0
    else:
        ok, sw_detail = _check_success_when(sw, returncode, stdout, stderr)
        detail = sw_detail + detail

    for expr in evaluation.get("assert") or []:
        passed, msg = _eval_assert(str(expr), merged)
        detail.append(f"assert '{expr}': {'ok' if passed else 'FAIL'} {msg}")
        ok = ok and passed

    return ok, merged, detail


def _subst_outputs(text: str, outputs: dict | None) -> str:
    """Substitute ``${name}`` / ``${outputs.name}`` from extracted outputs into *text*."""
    for k, v in (outputs or {}).items():
        text = text.replace(f"${{{k}}}", str(v)).replace(f"${{outputs.{k}}}", str(v))
    return text


def resolve_messages(evaluation: dict | None, ok: bool, outputs: dict | None) -> tuple[str, str]:
    """Resolve the human ``message`` and the agent ``prompt`` for a result.

    * ``message`` — outcome context. ``message_pass`` / ``message_fail`` override the
      generic ``message`` for that outcome.
    * ``prompt`` (alias ``comment``) — free-form content for an AI agent to explain or
      fix the step; surfaced to the run so agents/humans can act on it.
    Both support ``${extracted_name}`` substitution.
    """
    if not isinstance(evaluation, dict):
        return "", ""
    msg = evaluation.get("message_pass" if ok else "message_fail") or evaluation.get("message") or ""
    prompt = evaluation.get("prompt") or evaluation.get("comment") or ""
    return _subst_outputs(str(msg), outputs), _subst_outputs(str(prompt), outputs)
