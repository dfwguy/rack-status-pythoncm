#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# run-local-auth.sh — run Rack Status locally with the SRP authentication gate ON.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# `start.sh` disables the gate on macOS so day-to-day work needs no login. That default is right
# for developing a dashboard panel and useless for testing authentication, which is what this
# script is for: the gate enforced, a private identity store, and the bootstrap password printed
# where you can actually see it.
#
#   bin/run-local-auth.sh              # HTTPS, demo data, fresh store each run
#   KEEP_STORE=1 bin/run-local-auth.sh # reuse the previous run's store and password
#
# Then open the URL it prints and sign in with the credentials it shows.
#
# HTTPS ONLY, like every way this app serves. Testing authentication over http://localhost would
# hide two things that break on a real deployment:
#
#   * `Secure` cookies are not sent over plain HTTP at all, so the access and refresh cookies
#     silently do not come back and a session appears to end immediately;
#   * `crypto.subtle` — which the SRP client needs for SHA-256 — is only available in a secure
#     context. http://localhost happens to count as one, so it works locally and fails the first
#     time anyone reaches the dashboard by IP or hostname over plain HTTP.
#
# Both are the kind of defect that reproduces only on the deployment, which is one reason there is
# no plain-HTTP mode anywhere.

set -euo pipefail
cd "$(dirname "$0")/.."

STORE_DIR="${STORE_DIR:-var/local-auth}"
IDENTITY_FILE="$STORE_DIR/identity.json"

if [[ "${KEEP_STORE:-0}" != "1" ]]; then
  # A fresh store every run, because the password is emitted ONCE and is then unrecoverable by
  # design (FR-3). Reusing a store whose password you no longer have is a redeploy, which for a
  # local test is just confusing — so the default is to start clean.
  rm -rf "$STORE_DIR"
fi
mkdir -p "$STORE_DIR"

export RACK_STATUS_DEMO="${RACK_STATUS_DEMO:-1}"
export RACK_EXEC_MODE="${RACK_EXEC_MODE:-local}"
export NVIS_IDENTITY_FILE="$PWD/$IDENTITY_FILE"

# The policy file is isolated too, for the same reason as the identity store — and this one bit.
#
# The authorization engine reads `var/store/policies.json` by default. Any policy file left in the
# tree by earlier work therefore applies to this run, and because policies grant nothing until they
# are ATTACHED, a stray file means default-deny: you sign in successfully and then every request is
# refused. Observed on 2026-09-04, and it presents as a broken login rather than as a policy
# problem, which is a miserable thing to debug.
#
# Pointing at the private store dir means this script starts with no policy file, which is the
# permissive allow-all engine — the right default for testing authentication rather than
# authorization. To test authorization, write a policy through Identity > Policies and it lands in
# the private file, not the repo's.
export RACK_WEB_POLICY_FILE="${RACK_WEB_POLICY_FILE:-$PWD/$STORE_DIR/policies.json}"

# The gate is always on — there is no switch to set. This script predates that: it used to export
# `RACK_WEB_AUTH_DISABLED=0` to opt in, and said "use start.sh if you want it off", which is no
# longer true of start.sh either.

# The signing key must be stable across restarts or every token minted before a restart is refused
# (INV-17). app.py bootstraps one into var/secrets and that persists, so nothing is set here.

# Legacy variables are deliberately NOT set. They no longer authenticate anything, and exporting
# them would suggest otherwise. Unset rather than merely omitted, because a developer's shell may
# still carry them from an older checkout.
unset RACK_WEB_AUTH_TOKEN RACK_WEB_AUTH_MODE RACK_WEB_PAM_SERVICE RACK_WEB_AUTH_DISABLED \
      RACK_WEB_AUTH_USER 2>/dev/null || true

# ---- TLS -------------------------------------------------------------------------------------
#
# Nothing to do here. `start.sh` hands off to `cli.py serve`, whose gunicorn config settles the
# certificate through `startup_gate.ensure_tls()`: `config/tls/` if you put one there, otherwise a
# self-signed pair generated once under var/secrets/tls and reused, so the browser exception you
# accepted last run keeps applying.
SCHEME="https"
SCHEME_LABEL="HTTPS"

# ---- OIDC issuer ------------------------------------------------------------------------------
#
# Derived from the scheme and port this script is actually about to serve on, because those are the
# two things it knows and an exported value cannot. `iss` is compared by conforming clients as an
# exact string against the URL they fetched discovery from, so an `http://` issuer in front of an
# HTTPS listener produces tokens a discovery-driven client refuses -- while the app itself boots
# perfectly and logs the wrong issuer as though it were fine. Seen exactly that way on 2026-09-04:
# `issuer http://localhost:5050` logged by a run serving TLS on 5050.
#
# `setdefault`, not an override: a run pointing at a real issuer for interop testing keeps it. Note
# this is a *local development* default only -- a deployment must configure it, and the launch
# preflight refuses to start without it whenever the provider is enabled.
if [[ -z "${RACK_WEB_OIDC_ISSUER:-}" ]]; then
  export RACK_WEB_OIDC_ISSUER="${SCHEME}://localhost:${PORT:-5050}"
  ISSUER_NOTE="derived"
else
  ISSUER_NOTE="from the environment"
  case "$RACK_WEB_OIDC_ISSUER" in
    "${SCHEME}://"*) ;;
    *)
      echo "[run-local-auth] WARNING: RACK_WEB_OIDC_ISSUER is ${RACK_WEB_OIDC_ISSUER} but this run" >&2
      echo "[run-local-auth]          serves ${SCHEME}. Clients compare the issuer exactly, so they" >&2
      echo "[run-local-auth]          will reject these tokens. Unset it to have it derived." >&2
      ;;
  esac
fi

cat <<BANNER

  ─────────────────────────────────────────────────────────────────────
   NVIS Rack Status — local run, SRP authentication ENABLED, ${SCHEME_LABEL}
  ─────────────────────────────────────────────────────────────────────
   Watch for the ONE-TIME ADMINISTRATOR CREDENTIAL banner below and copy
   the password. It is not stored and cannot be shown again; only its
   SRP verifier is kept.

   Log in as: admin
   URL:       ${SCHEME}://localhost:${PORT:-5050}
   Issuer:    ${RACK_WEB_OIDC_ISSUER} (${ISSUER_NOTE})

   Unless config/tls/ holds your own certificate, it is self-signed, so the
   browser will warn once. Accept it and the exception persists — the cert
   is reused between runs, so the fingerprint does not change on restart.
  ─────────────────────────────────────────────────────────────────────

BANNER

exec ./start.sh "$@"
