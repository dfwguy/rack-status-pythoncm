---
diataxis: how-to
description: Set up config/rackstatus.toml, know which rung a value comes from, and see what a first boot creates for you.
---

# How to configure a deployment

Rack Status needs very little to start and refuses to start without three things it can create
itself: an administrator, a policy and a TLS certificate. This is the order to do it in.

The complete list of settings is [Configuration settings](../reference/configuration/settings.md),
generated from the source. Per-setting prose for the older half of them is in
`docs/ENVIRONMENT.md` in the repository.

## 1. Find out what a launch would do, before launching

```bash
python backend/cli.py check          # exits 78 on a fault
python backend/cli.py check --json   # same, for a deploy gate
```

`check` creates no administrator, no credential and no policy. It reports what a `serve` *would*
create and what it cannot.

It does seed the catalog, because resolving the required settings means resolving the active
cluster. That is a documented exception rather than an oversight — `test_cli.py` pins it.

## 2. Write the settings that cannot be guessed

Write the minimal file — only the head node, because everything else has a working default:

```bash
python3 backend/cli.py init-config --ssh-host <head-node>
# upgrading a deployment that used rack-config.env:
python3 backend/cli.py init-config --from-env-file rack-config.env
```

That writes `config/rackstatus.toml`. To set anything else, copy its line from
`config/rackstatus.toml.example`, which is generated from the settings registry and lists exactly
the settings a file may carry, in sections, with an example value each. Point somewhere else with
`NVIS_RACKSTATUS_CONFIG`.

A certificate of your own goes in `config/tls/server.crt` and `config/tls/server.key`; without
one, the first start generates a self-signed pair. There is no plain-HTTP mode.

For a real cluster the connection settings are the ones with no sensible default:

```toml
[connection]
RACK_SSH_HOST = "10.29.136.8"
RACK_SSH_USER = "cmods"
RACK_SSH_KEY = "~/.ssh/nvis_container"
RACK_REMOTE_BASE_PATH = "/home/cmsupport/nvis-rack-status"

[cluster]
RACK_PLATFORM = "bcm"
```

On a laptop, set `RACK_STATUS_DEMO = true` instead and none of the above is required.

Three refusals in the file are worth knowing before they surprise you:

- **Secrets are refused.** A setting whose name matches `PASSWORD|SECRET|TOKEN|API_KEY|CREDENTIAL`
  fails startup if it appears in the file, because the file is exactly the artifact that ends up
  in a support bundle and is not covered by `.gitignore` once copied. Put those in the
  environment.
- **Floats are refused.** Both float-shaped settings are version strings, and rendering one
  changes its meaning: `13.30` becomes `13.3`, and a shell comparison that splits on `.` reads a
  different number. Quote them.
- **An empty value is a value.** `FOO=""` already in the environment is not overwritten by the
  file, because several readers distinguish empty from unset.

## 3. Know which rung a value came from

A setting can arrive from four places. Highest wins:

| Rung | Source | Scope |
|---|---|---|
| 1 | the active cluster's **high overlay** — from the catalog | per cluster |
| 2 | **job env overrides** — `var/cache/job_env_overrides.json`, written by the Settings screen | per deployment |
| 3 | the **process environment** — `config/rackstatus.toml`, the shell, the container | per process |
| 4 | the platform's **low overlay** — geometry and per-tool defaults | per platform |

This is why a setting can be correct in `config/rackstatus.toml` and still not take effect: a cluster
record naming its own `RACK_SSH_HOST` outranks the file. The Settings screen shows the resolved
value, and [Configuration settings](../reference/configuration/settings.md) marks which settings
go through the ladder at all — a handful are read straight from the process environment and
cannot be set per cluster.

## 4. Start it

```bash
./start.sh                          # builds the frontend, then serves
python backend/cli.py serve --port 5050
```

`serve` takes the same path overrides as `check` (`--identity-file`, `--policy-file`,
`--catalog-file`, `--config`) and hands off to gunicorn, so a local run gets the same TLS floor
as production. The flags only write `os.environ`; precedence stays with the ladder above rather
than being resolved twice.

## What the first boot creates

Since 2026-09-08 the app refuses to serve without an administrator, an authorization policy and
TLS — and on a brand-new deployment it creates all three:

- an **administrator** with a one-time password, printed to the startup log and stored nowhere;
- the **`administrators` policy**, attached to that account, so a fresh install enforces
  default-deny rather than permitting everything;
- a **self-signed certificate**, because refusing to start for want of a certificate the product
  can make is a gate operators learn to route around.

Two consequences:

- **Copy the printed password.** It is re-issued on every restart until it is changed, which is
  what makes a missed credential recoverable — and what stops the printed one becoming a standing
  secret. Once changed, nothing is re-issued.
- **An existing store with no policy is refused, deliberately.** On a deployment that already
  holds accounts, nothing tells the product which of them should hold full access, so inventing a
  policy would either lock the operators out or hand somebody everything. `cli.py check` reports
  this before a restart discovers it.

## If a feature says "Access Denied" after an upgrade

The `administrators` policy is written once, at first boot, from the resource types that existed
then. A type added later is absent from it, and the symptom is an administrator being refused on
a feature that just shipped.

```bash
python backend/cli.py check                    # names the uncovered types
python backend/cli.py reconcile-admin-policy   # adds them
```

This is reported rather than repaired automatically, because a boot-time routine cannot tell
"this type is newer than the policy" from "an operator narrowed it deliberately", and silently
re-widening an administrators policy would be privilege escalation with good intentions.
