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
"""Grafana embed mechanics: which dashboard, which panel variables, which hosts, which timezone.

Shared by the parser-results charts, the live 5-minute rack charts, the deeplinks endpoint and settings.
Extracted because those four surfaces live in four different modules now, and an embed that disagrees
with itself between them is a support call ("the chart is empty on this page but not that one").

What makes this non-trivial is that a Grafana panel filters by HOSTNAME while the dashboard is addressed
by RACK, and the two are different namespaces (cluster-naming-conventions.mdc). So:

* ``_grafana_embed_host_regex_and_mode`` picks between a per-host regex and a rack-prefix regex and says
  WHICH it used, because "no data" looks identical whether the filter was too narrow or simply wrong.
* ``_job_hosts_for_grafana`` prefers the hosts sacct reports for the actual job, and only falls back to
  expanding the rack's validation target. The job's own host list is the truth; the expansion is a guess
  that a partially-allocated run makes wrong.
* ``_grafana_embed_rack_canonical_id`` normalises the rack id for the dashboard variable, since the
  dashboard's spelling is not always the dashboard id we use internally.

Timezone is deliberate too: a panel URL carries an explicit tz parameter rather than inheriting the
viewer's browser, so an operator in one timezone and one in another describe the same incident with the
same numbers.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# --- Grafana deep-links (open full BCM/NMC dashboards in a new tab, scoped by rack/host) --------
#
# Unlike the /d-solo/ *embeds* (which need GF_SECURITY_ALLOW_EMBEDDING and a homegrown 2-panel
# dashboard), deep-links open a full Grafana dashboard in a new tab. On a BCM 11 / Mission Control
# (NMC) head node these dashboards already exist and are scoped by the ``$Rack`` / ``$hostname``
# template variables — so we just build ``/d/<uid>/<slug>?var-<Rack>=..&var-<hostname>=..`` URLs.
# The catalog below defaults to the stock NMC GB200 dashboards but is fully overridable via
# ``RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS`` (JSON) so this is not hardwired to any one site.
from __future__ import annotations

import json
import logging
import re
import time

from config_env import _eget
from core import truthy_env as _truthy_env
from core import utc_iso_z as _iso_utc_z
from naming import _nvis_naming
from node_targets import (
    _hostnames_for_slurm_job,
    _scontrol_hostnames_preserving_case,
    validation_target_for_rack,
)
from slurm_query import _dedupe_hostnames_preserve_spelling

_logger = logging.getLogger(__name__)


def _hostnames_for_rack_validation_expand(rack: str) -> tuple[list[str], str]:
    try:
        tgt = validation_target_for_rack(rack, None)
    except ValueError:
        return [], "target_error"
    hs = _dedupe_hostnames_preserve_spelling(_scontrol_hostnames_preserving_case(tgt))
    return hs, ("rack_ct_expand" if hs else "rack_ct_expand_failed")


def _job_hosts_for_grafana(rack: str, job_raw: str | None) -> tuple[list[str], str]:
    hosts, src = _hostnames_for_slurm_job(job_raw)
    if hosts:
        return hosts, src
    hosts2, src2 = _hostnames_for_rack_validation_expand(rack)
    if hosts2:
        return hosts2, src2
    return [], "none"


def _grafana_bright_hostname_promql_regex(hosts: list[str]) -> str:
    """Build ``hostname=~`` RHS for Bright ``gpu_*`` metrics (Prometheus matches the **whole** label).

    Slurm ``NodeName`` casing may differ from Prometheus (Bright often lowercases ``uname -n``;
    DCGM may keep Slurm-style ``CBR01-...``). We emit **lowercase and verbatim** alternates when
    they differ so PromQL stays portable (no ``(?i)`` — VictoriaMetrics / some stacks reject it
    in label matchers, which would blank **every** branch including ``.*``).

    Per Slurm host we add: full name (with any domain), name before the first ``.``, a long final
    hyphen segment (≥6 chars, e.g. ``cn010211``), and the last two hyphen segments when distinct
    (e.g. ``svr-cn010211``). Each literal is escaped and followed by an optional FQDN suffix
    (parenthesized dot + any chars, quantified with ``?``); we avoid ``(?:`` because Grafana
    treats ``{`` in panel queries as template syntax and can break variables.

    ``RACK_WEB_GRAFANA_HOST_REGEX_STRICT=1``: ``|``-joined ``re.escape`` of each Slurm name
    (lowercase and verbatim when different; no short forms, no domain tail).
    """
    if not hosts:
        return ".*"
    if _truthy_env("RACK_WEB_GRAFANA_HOST_REGEX_STRICT"):
        parts_s: list[str] = []
        seen_s: set[str] = set()
        for h in hosts:
            raw = (h or "").strip()
            if not raw:
                continue
            variants = (raw.lower(), raw) if raw != raw.lower() else (raw,)
            for v in variants:
                pat = re.escape(v)
                if pat in seen_s:
                    continue
                seen_s.add(pat)
                parts_s.append(pat)
        return "|".join(parts_s) if parts_s else ".*"

    parts: list[str] = []
    seen_pat: set[str] = set()

    def add_pat_for_string(lit: str) -> None:
        raw = lit.strip()
        if not raw:
            return
        variants = (raw.lower(), raw) if raw != raw.lower() else (raw.lower(),)
        for v in variants:
            pat = re.escape(v) + r"(\..*)?"
            if pat in seen_pat:
                continue
            seen_pat.add(pat)
            parts.append(pat)

    for h in hosts:
        t = (h or "").strip()
        if not t:
            continue
        nodots = t.split(".", 1)[0].strip()
        add_pat_for_string(t)
        add_pat_for_string(nodots)
        if "-" in nodots:
            segs = nodots.split("-")
            last = segs[-1]
            if len(last) >= 6:
                add_pat_for_string(last)
            if len(segs) >= 2:
                two = f"{segs[-2]}-{segs[-1]}"
                if len(two) >= 6 and two.lower() != nodots.lower():
                    add_pat_for_string(two)
    if not parts:
        return ".*"
    return "|".join(parts)


def _grafana_embed_rack_canonical_id(rack: str, hosts: list[str]) -> str | None:
    """Resolve NVIS canonical rack id for Grafana ``<rack-id>.*`` embed filter."""
    n = _nvis_naming()
    rid = (rack or "").strip()
    rid_main = rid.split("(", 1)[0].strip()
    c = n.canonical_rack_id(rid_main)
    if c:
        return c
    for seg in rid_main.split("+"):
        seg = seg.strip()
        if not seg:
            continue
        cc = n.canonical_rack_id(seg)
        if cc:
            return cc
    for r2 in n.find_rack_ids_in_string(rid):
        cc = n.canonical_rack_id(r2)
        if cc:
            return cc
    fk = n.rack_id_from_nemo_1rack_filename(rid)
    if fk:
        return fk
    for h in hosts or ():
        hit = n.sinfo_rack_from_host(h)
        if hit:
            rr, _ = hit
            return n.canonical_rack_id(rr) or rr
    for h in hosts or ():
        for r2 in n.find_rack_ids_in_string(h):
            cc = n.canonical_rack_id(r2)
            if cc:
                return cc
    return None


def _grafana_rack_prefix_promql_regex(rack: str, hosts: list[str]) -> str | None:
    """PromQL label regex for Grafana embed: ``<canonical-rack-id>.*`` only.

    Matches full hostnames such as ``CBR01-DH01-SVR-CN010211``. This must match the form users
    verify in Grafana (e.g. ``CBR01-DH01-SVR-CN0102.*``): **no** ``re.escape`` on ordinary rack
    tokens (avoids ``\\-`` in URLs) and **no** ``|`` alternates — ``/d-solo/`` + text variables can
    fail when the regex contains a pipe. Do not emit ``{`` anywhere (Grafana template parsing).

    Set ``RACK_WEB_GRAFANA_EMBED_RACK_REGEX_LOWER=1`` to use lowercase ``<rack>.*`` when Prometheus
    labels are only lowercased.
    """
    rk = _grafana_embed_rack_canonical_id(rack, hosts)
    if not rk:
        return None
    if _truthy_env("RACK_WEB_GRAFANA_EMBED_RACK_REGEX_LOWER"):
        body = rk.lower()
    else:
        body = rk
    # Safe unescaped body: Prometheus RE2 treats ``-`` literally outside ``[]``.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,191}", body):
        return re.escape(body) + r".*"
    return body + r".*"


def _grafana_embed_host_regex_and_mode(rack: str, hosts: list[str]) -> tuple[str, str]:
    """``(regex, mode)`` for Grafana ``var-job_host_regex``. See ``RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST``."""
    if _truthy_env("RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST"):
        if hosts:
            return _grafana_bright_hostname_promql_regex(hosts), "hosts"
        return ".*", "open"
    pref = _grafana_rack_prefix_promql_regex(rack, hosts)
    if pref:
        return pref, "rack_prefix"
    if hosts:
        return _grafana_bright_hostname_promql_regex(hosts), "hosts"
    return ".*", "open"


_DISPLAY_TZ_QUERY_RE = re.compile(r"^[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*$")


def _sanitize_display_timezone_query(raw: object) -> str | None:
    """``display_timezone`` query for ``/api/parser-results`` (IANA). Reject junk / path tricks."""
    s = (raw if isinstance(raw, str) else str(raw or "")).strip()
    if not s or len(s) > 80 or ".." in s:
        return None
    if s.lower() == "browser":
        return None
    if not _DISPLAY_TZ_QUERY_RE.fullmatch(s):
        return None
    return s


def _grafana_dash_uid() -> str:
    return (_eget("RACK_WEB_GRAFANA_DASH_UID") or "bright-gpu-per-host").strip() or "bright-gpu-per-host"


def _grafana_dash_slug() -> str:
    s = (_eget("RACK_WEB_GRAFANA_DASH_SLUG") or "bright-gpu-thermal-and-power").strip()
    return s or "bright-gpu-thermal-and-power"


def _rack_web_grafana_uri() -> str:
    u = (_eget("RACK_WEB_GRAFANA_URI") or "").strip()
    return u.rstrip("/") if u else ""


def _rack_web_grafana_port() -> str:
    p = (_eget("RACK_WEB_GRAFANA_PORT") or "3000").strip() or "3000"
    return p


_GRAFANA_DEEPLINK_DEFAULT: list[dict[str, str]] = [
    {
        "id": "dcgm",
        "label": "GPU / DCGM",
        "uid": "f4f4474d-f3e1-4e73-a2dc-33c52cb07bd4",
        "slug": "02-dgx-gb200-dcgm-dashboard",
        "scope": "host",
        "desc": "GPU util, temp, power, NVLink BW, XID errors, HBM (per node/GPU).",
    },
    {
        "id": "power",
        "label": "Rack power & thermal",
        "uid": "2394651a-f07b-42d4-b0ff-affa31d0a254",
        "slug": "05-dgx-gb200-rack-power-analysis",
        "scope": "rack",
        "desc": "Rack-level power draw and thermal analysis.",
    },
    {
        "id": "nvlink",
        "label": "NVLink",
        "uid": "0f0286bc-b007-4a38-a359-8384ee11a90a",
        "slug": "10-dgx-gb200-nvlink-dashboard",
        "scope": "host",
        "desc": "NVLink / NVSwitch fabric bandwidth and errors.",
    },
    {
        "id": "health",
        "label": "Health status",
        "uid": "16a8973d-e9fd-4cdf-9649-b2041705a810",
        "slug": "04-dgx-gb200-health-status",
        "scope": "rack",
        "desc": "GB200 node/GPU health status roll-up.",
    },
    {
        "id": "slurm",
        "label": "Slurm",
        "uid": "006a04e0-fd06-4ba4-9d44-251394198796",
        "slug": "06-slurm-dashboard",
        "scope": "cluster",
        "desc": "Slurm queue, allocation and node-state overview.",
    },
    {
        "id": "overall",
        "label": "SuperPOD overview",
        "uid": "b0dfdff4-f570-47a9-949a-3ab535474c2b",
        "slug": "01-superpod-overall-dashboard",
        "scope": "cluster",
        "desc": "Cluster-wide overview (all racks).",
    },
]


def _grafana_var_rack() -> str:
    return (_eget("RACK_WEB_GRAFANA_VAR_RACK") or "Rack").strip() or "Rack"


def _grafana_var_host() -> str:
    return (_eget("RACK_WEB_GRAFANA_VAR_HOST") or "hostname").strip() or "hostname"


def _grafana_deeplinks_enabled() -> bool:
    """Deep-links are enabled unless explicitly disabled; a base URL is required to be useful."""
    return not _truthy_env("RACK_WEB_GRAFANA_DEEPLINK_DISABLED")


def _grafana_deeplink_dashboards() -> list[dict[str, str]]:
    """Parsed deep-link dashboard catalog.

    Override with ``RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS`` = JSON list of objects, each with
    ``uid`` (required), ``label``, ``slug``, ``scope`` (``host``|``rack``|``cluster``) and
    ``desc``. Invalid JSON or a non-list falls back to the built-in NMC catalog.
    """
    raw = (_eget("RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS") or "").strip()
    if not raw:
        return [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        _logger.warning("RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS is not valid JSON; using defaults")
        return [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]
    if not isinstance(parsed, list):
        return [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]
    out: list[dict[str, str]] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip()
        if not uid:
            continue
        scope = str(item.get("scope") or "rack").strip().lower()
        if scope not in ("host", "rack", "cluster"):
            scope = "rack"
        out.append(
            {
                "id": str(item.get("id") or f"dash{i}").strip() or f"dash{i}",
                "label": str(item.get("label") or uid).strip() or uid,
                "uid": uid,
                "slug": str(item.get("slug") or "d").strip() or "d",
                "scope": scope,
                "desc": str(item.get("desc") or "").strip(),
            }
        )
    return out or [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]


def _grafana_url_timezone_param() -> str:
    """Value for Grafana ``timezone`` query param and parser UI chart labels.

    ``RACK_WEB_GRAFANA_TIMEZONE``: ``browser``, ``utc``, or an IANA zone (e.g. ``America/Los_Angeles``).
    When unset, use the same zone as log timestamp parsing (``RACK_PROMETHEUS_LOG_TIMESTAMP_TZ``),
    normalised to ``utc`` for UTC-like names so panel axes match server-derived ``from``/``to``.
    """
    g = (_eget("RACK_WEB_GRAFANA_TIMEZONE") or "").strip()
    if g:
        gl = g.lower()
        if gl in ("browser", "utc"):
            return gl
        return g
    logtz = (_eget("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC"
    if logtz.upper() in ("UTC", "GMT", "Z", "UCT"):
        return "utc"
    return logtz


def _effective_display_tz_for_embed(log_tz_query: str | None) -> str:
    """Grafana ``timezone=`` and ``display_timezone`` when user passes ``display_timezone`` query."""
    if log_tz_query and log_tz_query.strip():
        q = log_tz_query.strip()
        if q.lower() in ("utc", "gmt", "z", "uct"):
            return "utc"
        return q
    return _grafana_url_timezone_param()


def _grafana_live_5m_embed_context(
    rack: str,
    log_timestamp_tz: str | None = None,
) -> dict[str, object]:
    """Grafana solo embed for the last five minutes (wall clock), hosts = rack CT expand."""
    dtz = _effective_display_tz_for_embed(log_timestamp_tz)
    if _truthy_env("RACK_WEB_GRAFANA_EMBED_DISABLED"):
        return {
            "ok": False,
            "iframe_ready": False,
            "disabled": True,
            "note": "Grafana embed disabled (RACK_WEB_GRAFANA_EMBED_DISABLED).",
            "display_timezone": dtz,
        }
    now = time.time()
    lo, hi = now - 300.0, now
    if hi <= lo:
        return {
            "ok": False,
            "iframe_ready": False,
            "note": "Invalid time window (end <= start).",
            "display_timezone": dtz,
        }
    hosts, hsrc = _hostnames_for_rack_validation_expand(rack)
    host_regex, host_regex_mode = _grafana_embed_host_regex_and_mode(rack, hosts)
    return {
        "ok": True,
        "iframe_ready": True,
        "from_ms": int(lo * 1000),
        "to_ms": int(hi * 1000),
        "from_iso_utc": _iso_utc_z(lo),
        "to_iso_utc": _iso_utc_z(hi),
        "host_regex": host_regex,
        "host_regex_mode": host_regex_mode,
        "hosts": hosts,
        "hosts_source": hsrc,
        "time_source": "live_last_5m",
        "dashboard_uid": _grafana_dash_uid(),
        "dash_slug": _grafana_dash_slug(),
        "panel_ids": [1, 2],
        "display_timezone": dtz,
    }
