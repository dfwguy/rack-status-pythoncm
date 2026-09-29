<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary
-->
# `nvrs` — NVIDIA Rack Status CLI

A single-binary client for a Rack Status deployment: run tests, validations and reports, and manage
a cluster, from the terminal. It is a **client of the existing API** — it speaks the same
`/api/*` contract, SRP login and token auth as the web UI, and adds no backend surface of its own.

> Status: **Phase 1**. Profiles, SRP-6a `login`/`logout`, a token store with automatic refresh, a
> generic `nvrs api` passthrough that reaches the whole API, and `whoami`. Typed domain verbs and the
> interactive terminal land in later phases — see the build plan.

## Workspace layout

| Crate | Role |
| --- | --- |
| `nvrs` | the binary: clap command tree, output rendering, exit codes |
| `nvrs-client` | transport — HTTP + streaming, the JSON envelope, 429 backoff, TLS trust |
| `nvrs-auth` | SRP-6a login, the token store, and refresh |
| `nvrs-openapi` | fetch/cache and query the live OpenAPI contract *(Phase 2)* |

## Build & test

```bash
cd cli
cargo build
cargo test --all
```

CI gates (matched by `.gitlab-ci.yml`):

```bash
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
```

## Configure a deployment

Clusters must never be mixed, so each is a named **profile**; the active profile is explicit.

```bash
nvrs config set-url mayo https://localhost:5000   # first profile becomes active
nvrs config set-operator mayo "Your Name"
nvrs config set-tls mayo /etc/nvrs/mayo-ca.pem     # pin the OVA's self-signed cert
#   ... or:  nvrs config set-tls mayo insecure      (skip verification — first contact only)
nvrs config list
```

The OVA serves HTTPS only, usually reached over an ssh tunnel (`ssh -L 5000:localhost:5000 …`), so
the base URL is typically `https://localhost:<local-port>`. Config lives at
`$XDG_CONFIG_HOME/nvrs/config.toml` (or `~/.config/nvrs/config.toml`); `$NVRS_CONFIG` overrides it.
It holds **no secrets** — session tokens live in a separate `0600` state file (see below).

## Log in

SRP-6a: the password never leaves the machine — the server receives a proof, not the password. The
session (an access token + a refresh token) is stored under the profile name in a `0600` file at
`$XDG_STATE_HOME/nvrs/tokens.json` (or `~/.local/state/nvrs/tokens.json`; `$NVRS_STATE` overrides).
Calls refresh the access token automatically when it expires; a dead refresh token prompts re-login.

```bash
nvrs login                      # prompts for login + password (hidden)
nvrs login operator             # login given, password prompted
echo "$PW" | nvrs login operator --password-stdin   # scripted / CI
nvrs whoami                     # who am I on the active profile?
nvrs logout                     # revoke the session and forget it
```

An access token can still be supplied out-of-band with `$NVRS_TOKEN` (or `nvrs api --token`) — handy
for a short-lived CI credential — which bypasses the stored session.

## Call the API

```bash
nvrs api GET /api/status
nvrs api GET /api/rack-locks
nvrs api POST /api/rack-locks/p17-r01 --field note="burn-in" --field ttl_sec=3600
nvrs api POST /api/run-validation-async --body @params.json
nvrs api POST /api/tools/download-containers-stream --field arch=x86_64 --stream

nvrs --json api GET /api/status | jq .        # machine-readable envelope for scripting
```

Exit codes distinguish *why* a call failed: `77` not authenticated / forbidden, `75` rate-limited
(retry later), `69` could not reach the deployment, `1` everything else.
