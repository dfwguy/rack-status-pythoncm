<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary
-->
# `nvrs` — NVIDIA Rack Status CLI

A single-binary client for a Rack Status deployment: run tests, validations and reports, and manage
a cluster, from the terminal. It is a **client of the existing API** — it speaks the same
`/api/*` contract, SRP login and token auth as the web UI, and adds no backend surface of its own.

> Status: **Phase 0** (foundation). Profiles, a generic `nvrs api` passthrough that reaches the whole
> API, and a `whoami` probe. SRP `login` and typed domain verbs land in later phases — see the build
> plan. Until `login` exists, pass an access token via `$NVRS_TOKEN`.

## Workspace layout

| Crate | Role |
| --- | --- |
| `nvrs` | the binary: clap command tree, output rendering, exit codes |
| `nvrs-client` | transport — HTTP + streaming, the JSON envelope, 429 backoff, TLS trust |
| `nvrs-auth` | SRP-6a login and the token store *(Phase 1)* |
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
It holds **no secrets** — tokens go in the OS keyring (Phase 1).

## Call the API

```bash
export NVRS_TOKEN=<access-token>            # bridge until `nvrs login` exists

nvrs api GET /api/status
nvrs api GET /api/rack-locks
nvrs api POST /api/rack-locks/p17-r01 --field note="burn-in" --field ttl_sec=3600
nvrs api POST /api/run-validation-async --body @params.json
nvrs api POST /api/tools/download-containers-stream --field arch=x86_64 --stream

nvrs --json api GET /api/status | jq .        # machine-readable envelope for scripting
```

Exit codes distinguish *why* a call failed: `77` not authenticated / forbidden, `75` rate-limited
(retry later), `69` could not reach the deployment, `1` everything else.
