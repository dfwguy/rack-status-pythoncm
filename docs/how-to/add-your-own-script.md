---
diataxis: how-to
description: Put a script where the app will find it, then describe it in the Tool Registry.
---

# How to add your own script and register it as a tool

Register a script you wrote so it appears as an action in the UI. Two steps: put the file where
the app will find it, then describe it in the Tool Registry.

The reasoning behind the directories is in
[How a script path is chosen](../explanation/how-a-script-path-is-chosen.md). This page is the
procedure.

## 1. Choose the directory

| Your script | Put it in |
|---|---|
| You wrote it for this deployment | `{base}/scripts/custom/` |
| It changes one project's behaviour only | `{base}/scripts/project-<project-id>/` |
| It should ship with the app | open a merge request against `bin/scripts/` |

`{base}` is `RACK_REMOTE_BASE_PATH` on the head node — `/home/cmsupport/nvis-rack-status` by
default. The Projects screen shows the resolved paths for the active project.

Do not put it in `multi-platform/` or a `<platform-id>/` directory. Those ship with the app,
every project on this server shares them, and a deploy overwrites them.

## 2. Copy the script to the head node

```bash
scp my_probe.sh <head>:/home/cmsupport/nvis-rack-status/scripts/custom/
ssh <head> chmod +x /home/cmsupport/nvis-rack-status/scripts/custom/my_probe.sh
```

Create `scripts/custom/` first if this is the deployment's first custom script:

```bash
ssh <head> mkdir -p /home/cmsupport/nvis-rack-status/scripts/custom
```

## 3. Register it in the Tool Registry

1. Open **Platform → Tool Registry** and click **New tool**.
2. Set **Script** to the *basename only* — `my_probe.sh`, not a path. The app resolves it
   through the layers.
3. Set **Category** and **Surfaces** (for example `rackDetail.hardware`) to decide where the
   action appears.
4. Set **Platform compatibility**: all platforms, required capabilities, or specific platform
   ids. This decides which clusters are offered the tool; it does not affect which copy of the
   script runs.
5. Set **Execution mode** and **target** (`async`/`sync`/`stream`, and `rack`/`node`/`cluster`).
6. Save.

The action appears immediately in **Add action** on the rack boards — the registry announces the
write, so the boards refetch without a page reload.

## 4. Check it resolves

`GET /api/catalog/projects/<project-id>/scripts` reports the resolved layout, including which
directory each layer points at and how far the project's copy has drifted from the defaults. In
the UI, the Projects screen's **Update scripts** button reports the same thing.

If the launch fails with *no such file*, the basename is not in any layer for the active project.
Confirm the file is on the **head node** — not on the machine running the dashboard — and that
its name matches exactly.

## If your script is a compiled binary

A basename only resolves inside the script tree, so a binary that lives in a workspace — a
`nccl-tests` build, for instance — will not be found there. Two options:

- Wrap it in a script that locates the binary, as `nccl_allreduce.sbatch` does with
  `NVIS_NCCL_TESTS_BIN`, and register the wrapper. Preferred: the wrapper is also where the
  fabric environment belongs.
- Give the tool an **Env key override** and set that variable to the absolute path of the binary.

## Getting your script into the product

If the script is useful to more than this deployment, open a merge request adding it to
`bin/scripts/` with a short description of what it does and when to run it. Once merged it
arrives in `multi-platform/` the next time a deployment takes the shipped scripts — either
"Deploy scripts to head" on the Projects screen, or `bin/deploy-scripts.sh` — and nobody has to
copy it by hand again.
