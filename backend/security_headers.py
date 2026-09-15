# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Response security headers: CSP, HSTS, and the small set of always-on hardening headers.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The app served **no** security headers at all before this module: no CSP, no
``X-Content-Type-Options``, no ``Referrer-Policy``, no frame policy. CSP is the threat model's
own stated mitigation for T-7 (a compromised browser reaching the terminal and agent surfaces),
and it is the half of "browsers get stricter over HTTPS" that actually needs code -- CORS does
not, because the SPA and its API are one origin (Flask serves the bundle in production, and the
Vite dev server proxies ``/api`` in development).

Pure functions taking explicit inputs, so the policy can be asserted without a request context.
``app.py`` owns the ``after_request`` that applies them.

**Deliberately not CORS.** ``Access-Control-Allow-Origin: *`` is illegal with credentials, and a
credentialed allowlist would give away the same-origin protection this app gets for free. If the
Vite dev server ever needs a remote backend, extend its proxy instead.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: How long a browser should remember to use HTTPS for this origin. Six months.
#:
#: No ``includeSubDomains`` and no ``preload``: these deployments are reached by IP or by a
#: site-internal name, so claiming authority over sibling names would be both meaningless and
#: a way to break an unrelated service on the same domain. Emitted **only** on a request that
#: actually arrived over TLS -- sending it on plain HTTP is how a dashboard makes itself
#: unreachable for the lifetime of the header.
HSTS_MAX_AGE = 15552000


def _origin(url: str) -> str:
    """``scheme://host[:port]`` from a URL, or "" when it has no scheme+host."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def grafana_frame_origin(
    *,
    explicit_uri: str = "",
    page_scheme: str = "http",
    page_host: str = "",
    port: str = "3000",
) -> str:
    """The origin the browser will use for an embedded Grafana panel, or "".

    Mirrors ``frontend/src/lib/grafanaLinks.ts``: an explicit ``RACK_WEB_GRAFANA_URI`` wins,
    otherwise the panel is assumed to be on the same host as the page at *port*. The two must
    agree, or CSP blocks the iframe -- which looks exactly like the *other* Grafana failure
    under TLS (mixed content), so getting this wrong means debugging the same blank panel twice
    for two different reasons.
    """
    explicit = _origin(explicit_uri)
    if explicit:
        return explicit
    host = (page_host or "").split(":", 1)[0].strip()
    if not host:
        return ""
    p = (port or "3000").strip() or "3000"
    return f"{page_scheme}://{host}:{p}"


def content_security_policy(
    *,
    frame_ancestors: str = "'none'",
    page_scheme: str = "http",
    page_host: str = "",
    grafana_origin: str = "",
    extra_origins: tuple[str, ...] = (),
) -> str:
    """The dashboard's CSP.

    Tight where it matters and explicit about every relaxation:

    * ``script-src 'self'`` with no ``unsafe-inline`` -- the whole point. Verified against the
      built bundle, which has zero inline ``<script>`` tags (Vite emits a module script plus
      modulepreload links) and references no external host.
    * ``style-src`` needs ``'unsafe-inline'``. MUI/emotion and xterm inject ``<style>``
      elements at runtime, and there is no nonce plumbing through the SPA. This weakens CSS
      injection defence only; it does not permit script.
    * ``img-src`` needs ``data:`` and ``blob:`` for Chart.js canvas exports and mermaid SVG.
    * ``worker-src blob:`` because bundlers ship workers as blobs.
    * ``connect-src`` carries the WebSocket origin explicitly. ``'self'`` is treated
      inconsistently for ``ws:``/``wss:`` across browser versions, and getting it wrong breaks
      the terminal rather than anything obvious.
    * ``frame-ancestors 'none'`` -- nothing should frame this dashboard.

      *frame_ancestors* relaxes that for one case: the documentation site, which the app shows in
      an iframe. `'none'` blocks **all** framing including same-origin, so the in-app
      documentation view rendered an empty box in a real browser while every test passed --
      jsdom does not enforce the header. Passing `'self'` for `/docs/` is narrower than dropping
      it: the docs are public static files built from this repository, so same-origin framing of
      them adds no surface, while every other response stays unframeable.
    * ``frame-src`` is ``'self'`` plus Grafana when configured -- ``'self'`` because the app
      frames its own documentation, and an unnamed origin must render as a value rather than
      an empty directive, which some browsers treat as "allow".
    """
    ws_scheme = "wss" if page_scheme == "https" else "ws"
    connect = ["'self'"]
    if page_host:
        connect.append(f"{ws_scheme}://{page_host}")
    connect.extend(extra_origins)

    # `'self'` **and** the Grafana origin. This read `grafana_origin or "'none'"`, which is the
    # third directive in a set of three that all have to agree before an iframe renders, and the
    # one I missed: `frame-ancestors` and `X-Frame-Options` govern who may frame the *docs*,
    # while `frame-src` governs what the *app page* may embed. With `'none'` here the browser
    # blocked the in-app documentation silently -- a white rectangle, no console error the
    # operator would see, and both of the other two directives correct.
    #
    # `'self'` is the narrowest value that works: the app may frame its own origin, which is the
    # documentation and nothing else, since every other embed target is a separate origin that
    # still has to be named here.
    frame_src = " ".join(["'self'"] + ([grafana_origin] if grafana_origin else []))

    directives = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        f"frame-ancestors {frame_ancestors}",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        f"connect-src {' '.join(connect)}",
        f"frame-src {frame_src}",
        "worker-src 'self' blob:",
    ]
    return "; ".join(directives)


def security_headers(
    *,
    is_secure: bool,
    frame_ancestors: str = "'none'",
    page_scheme: str = "http",
    page_host: str = "",
    grafana_origin: str = "",
    csp_mode: str = "enforce",
    extra_connect_origins: tuple[str, ...] = (),
) -> dict[str, str]:
    """Headers to add to every response.

    *csp_mode* is ``enforce`` (default), ``report`` (send
    ``Content-Security-Policy-Report-Only``, which logs violations in the console and blocks
    nothing) or ``off``. ``report`` exists so a new deployment can confirm the policy fits its
    Grafana setup before enforcing it; ``off`` is the one-line rollback if a policy turns out to
    break a browser we have not tested.
    """
    out = {
        # The only header here that is purely about *not* guessing: without it a response whose
        # content type is wrong can be re-interpreted as script.
        "X-Content-Type-Options": "nosniff",
        # Do not leak a dashboard URL (which carries rack and job ids) to another origin.
        "Referrer-Policy": "strict-origin-when-cross-origin",
        # Legacy sibling of frame-ancestors, for browsers that honour only this one.
        # Legacy sibling of frame-ancestors, for browsers that honour only this one. It has to
        # move with `frame_ancestors` or the two disagree and the stricter one wins -- which is
        # how "the iframe is empty and the CSP looks fine" happens.
        "X-Frame-Options": "SAMEORIGIN" if frame_ancestors != "'none'" else "DENY",
    }
    mode = (csp_mode or "enforce").strip().lower()
    if mode != "off":
        policy = content_security_policy(
            frame_ancestors=frame_ancestors,
            page_scheme=page_scheme,
            page_host=page_host,
            grafana_origin=grafana_origin,
            extra_origins=extra_connect_origins,
        )
        key = "Content-Security-Policy-Report-Only" if mode == "report" else "Content-Security-Policy"
        out[key] = policy
    if is_secure:
        out["Strict-Transport-Security"] = f"max-age={HSTS_MAX_AGE}"
    return out
