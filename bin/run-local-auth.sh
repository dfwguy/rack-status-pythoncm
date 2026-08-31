#!/usr/bin/env bash
# run-local-auth.sh — run Rack Status locally with the SRP authentication gate ON.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# `start.sh` disables the gate on macOS so day-to-day work needs no login. That default is right
# for developing a dashboard panel and useless for testing authentication, which is what this
# script is for: the gate enforced, a private identity store, and the bootstrap password printed
# where you can actually see it.
#
#   bin/run-local-auth.sh              # demo data, fresh store each run
#   KEEP_STORE=1 bin/run-local-auth.sh # reuse the previous run's store and password
#
# Then open the URL it prints and sign in with the credentials it shows.

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

# The gate ON. This is the whole point of the script, so it is not overrideable by accident —
# use start.sh if you want it off.
export RACK_WEB_AUTH_DISABLED=0

# The signing key must be stable across restarts or every token minted before a restart is refused
# (INV-17). app.py bootstraps one into var/secrets and that persists, so nothing is set here.

# Legacy variables are deliberately NOT set. They no longer authenticate anything, and exporting
# them would suggest otherwise.
unset RACK_WEB_AUTH_TOKEN RACK_WEB_AUTH_MODE RACK_WEB_PAM_SERVICE 2>/dev/null || true

cat <<'BANNER'

  ─────────────────────────────────────────────────────────────────────
   Rack Status — local run with SRP authentication ENABLED
  ─────────────────────────────────────────────────────────────────────
   Watch for the ONE-TIME ADMINISTRATOR CREDENTIAL banner below and copy
   the password. It is not stored and cannot be shown again; only its
   SRP verifier is kept.

   Log in as: admin
  ─────────────────────────────────────────────────────────────────────

BANNER

exec ./start.sh "$@"
