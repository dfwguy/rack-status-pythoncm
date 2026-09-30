<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary
-->
# `nvrs` — NVIDIA Rack Status CLI

A single-binary client for a Rack Status deployment: run tests, validations and reports, and manage
a cluster, from the terminal. It is a **client of the existing API** — it speaks the same
`/api/*` contract, SRP login and token auth as the web UI, and adds no backend surface of its own.

> Status: **Phase 4**. Profiles, SRP-6a `login`/`logout` with automatic refresh, `whoami`, OpenAPI
> discovery (`nvrs spec`), the generic `nvrs api` passthrough, typed verbs (`compute status`, `rack
> list/lock/unlock`, `run <suite> --follow`, `reports list`), live log tailing (`logs tail`), and an
> interactive shell over the web terminal (`nvrs terminal`). Admin CRUD lands in Phase 5.

## Workspace layout

| Crate | Role |
| --- | --- |
| `nvrs` | the binary: clap command tree, output rendering, exit codes |
| `nvrs-client` | transport — HTTP + streaming, the JSON envelope, 429 backoff, TLS trust |
| `nvrs-auth` | SRP-6a login, the token store, and refresh |
| `nvrs-openapi` | fetch/cache and query the live OpenAPI contract |

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

## Typed verbs

Curated commands over the flat API, rendered as tables (`--json` for the raw envelope):

```bash
nvrs compute status                 # the main rack status payload
nvrs rack list                      # who holds which rack
nvrs rack lock p17-r01 --note "burn-in" --ttl 3600   # needs an operator on the profile
nvrs rack unlock p17-r01
nvrs reports list                   # generated reports kept on disk

nvrs run                            # list the runnable suites
nvrs run validation --rack p17-r01              # submit, print the job id
nvrs run validation --rack p17-r01 --follow     # submit and poll to completion
```

`run --follow` prints each phase change (`[running]` → `[done]`) and **exits with the job's result**
— non-zero if the job failed or its script returned non-zero — so it can gate a shell script.
Suite-specific parameters go through `--field key=value` or `--body @params.json`, the same as
`nvrs api`.

## Live output

```bash
nvrs logs tail --dir /path/to/run/logs             # tail -f the run log
nvrs logs tail --dir /path/to/logs --job-path "$STDOUT"   # a Slurm job's stdout
nvrs terminal --node p17-r01-n04                   # interactive shell on a node
nvrs terminal --headnode                           # …or the cluster head node
```

`nvrs terminal` opens the deployment's web terminal — the backend spawns `ssh` to the target and
bridges its PTY over a WebSocket. The local terminal goes into raw mode and is restored on exit;
window resizes are forwarded, and exiting the remote shell disconnects. Needs a real TTY.

## Discover the API

`nvrs spec` reads the deployment's own OpenAPI 3.1 document (generated from its route table, so it
never drifts) and caches it under `$XDG_CACHE_HOME/nvrs/` (`$NVRS_CACHE` overrides). `nvrs api` then
uses that cache to warn — never block — when a call names an unknown path or a method the endpoint
does not define (`--no-validate` skips the check).

```bash
nvrs spec                       # every endpoint: METHOD PATH — summary
nvrs spec rack                  # filter by method/path/summary substring
nvrs spec rack-locks --details  # parameters and tags for matching endpoints
nvrs spec --refresh             # re-fetch instead of using the cache
nvrs --json spec | jq '.paths | keys'   # the raw OpenAPI document
```

Exit codes distinguish *why* a call failed: `77` not authenticated / forbidden, `75` rate-limited
(retry later), `69` could not reach the deployment, `1` everything else.
