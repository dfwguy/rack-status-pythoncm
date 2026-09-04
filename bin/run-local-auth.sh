#!/usr/bin/env bash
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
#   TLS=0 bin/run-local-auth.sh        # plain HTTP, if you need to watch the wire
#
# Then open the URL it prints and sign in with the credentials it shows.
#
# HTTPS IS THE DEFAULT HERE, unlike start.sh, and the reason is specific to what this script is
# for. Testing authentication over http://localhost hides two things that break the moment a real
# deployment serves TLS:
#
#   * `Secure` cookies are not sent over plain HTTP at all, so the access and refresh cookies
#     silently do not come back and a session appears to end immediately;
#   * `crypto.subtle` — which the SRP client needs for SHA-256 — is only available in a secure
#     context. http://localhost happens to count as one, so it works locally and fails the first
#     time anyone reaches the dashboard by IP or hostname over plain HTTP.
#
# Both are the kind of defect that reproduces only on the deployment, so this script serves TLS by
# default and generates the certificate if it is missing.

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

# The gate ON. This is the whole point of the script, so it is not overrideable by accident —
# use start.sh if you want it off.
export RACK_WEB_AUTH_DISABLED=0

# The signing key must be stable across restarts or every token minted before a restart is refused
# (INV-17). app.py bootstraps one into var/secrets and that persists, so nothing is set here.

# Legacy variables are deliberately NOT set. They no longer authenticate anything, and exporting
# them would suggest otherwise.
unset RACK_WEB_AUTH_TOKEN RACK_WEB_AUTH_MODE RACK_WEB_PAM_SERVICE 2>/dev/null || true

# ---- TLS -------------------------------------------------------------------------------------
#
# `start.sh` serves TLS under gunicorn whenever both variables are set, so this only has to make
# sure a certificate exists and point at it. Deliberately NOT the Werkzeug dev server: it emits
# chunked, brotli-compressed bodies, and over TLS that is a shutdown race (see start.sh).
TLS_DIR="${TLS_DIR:-var/secrets/tls}"
TLS_CERT="$PWD/$TLS_DIR/server.crt"
TLS_KEY="$PWD/$TLS_DIR/server.key"

if [[ "${TLS:-1}" == "1" ]]; then
  if [[ ! -f "$TLS_CERT" || ! -f "$TLS_KEY" ]]; then
    echo "[run-local-auth] no certificate at $TLS_DIR — generating a self-signed one"

    # The repo venv, not the system python3. `cryptography` is a backend dependency and is not
    # generally installed system-wide, so `python3 bin/gen-tls-cert.py` fails with the generator's
    # own "cryptography is not importable" message — which tells you to use the venv. Doing that
    # here means the script does not hand you an error it already knows how to avoid.
    #
    # start.sh creates the venv, so on a first-ever run it may not exist yet; fall back to
    # python3 and let the generator's message stand.
    TLS_PY="${PYTHON:-}"
    if [[ -z "$TLS_PY" ]]; then
      if [[ -x .venv/bin/python ]]; then TLS_PY=.venv/bin/python; else TLS_PY=python3; fi
    fi

    # Not --force: an existing cert is reused across runs on purpose. Re-issuing every time would
    # change the fingerprint, so the browser exception you accepted last run stops applying and
    # you get the interstitial again on every restart.
    if ! "$TLS_PY" bin/gen-tls-cert.py --out-dir "$TLS_DIR" >/dev/null; then
      echo "[run-local-auth] STOP: could not generate a certificate." >&2
      echo "[run-local-auth] Run ./start.sh once to create the venv, or use TLS=0 for plain HTTP." >&2
      exit 1
    fi
  fi

  # Both, or neither. A cert without its key downgrades one path silently and crashes the other,
  # which is why the launch preflight requires the key whenever the cert is set.
  export RACK_WEB_TLS_CERT="$TLS_CERT"
  export RACK_WEB_TLS_KEY="$TLS_KEY"
  SCHEME="https"
  SCHEME_LABEL="HTTPS"
else
  # Explicit opt-out, for watching the wire in a proxy. Unset rather than empty: start.sh tests
  # for a non-empty value, and an inherited variable from a previous shell would otherwise turn
  # TLS back on while this script claims it is off.
  unset RACK_WEB_TLS_CERT RACK_WEB_TLS_KEY
  SCHEME="http"
  SCHEME_LABEL="plain HTTP"
fi

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
   NVIS Greenlight — local run, SRP authentication ENABLED, ${SCHEME_LABEL}
  ─────────────────────────────────────────────────────────────────────
   Watch for the ONE-TIME ADMINISTRATOR CREDENTIAL banner below and copy
   the password. It is not stored and cannot be shown again; only its
   SRP verifier is kept.

   Log in as: admin
   URL:       ${SCHEME}://localhost:${PORT:-5050}
   Issuer:    ${RACK_WEB_OIDC_ISSUER} (${ISSUER_NOTE})
$(
  if [[ "$SCHEME" == "https" ]]; then
    cat <<'TLSNOTE'

   The certificate is self-signed, so the browser will warn once. Accept
   it and the exception persists — the cert is reused between runs, so
   the fingerprint does not change on restart.
TLSNOTE
  else
    cat <<'HTTPNOTE'

   Plain HTTP (TLS=0). Two things behave differently from a deployment:
   Secure cookies are not sent, and crypto.subtle is only available in a
   secure context — http://localhost counts as one, an IP or hostname
   does not.
HTTPNOTE
  fi
)
  ─────────────────────────────────────────────────────────────────────

BANNER

exec ./start.sh "$@"
