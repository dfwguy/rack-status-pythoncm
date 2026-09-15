---
diataxis: reference
description: Every field of a Tool document, what it controls, and the values it accepts.
---

# Tool documents

A **Tool** is how a script becomes something the UI can run. One document per script, held in
the catalog, so shipping a new test or bumping a container version is a catalog edit rather than
a release.

This is the field reference. The values listed are the ones the catalog validates against, taken
from `backend/catalog/models.py`; a write that names anything else is refused by the API.

## Identity

| Field | Type | Purpose |
|---|---|---|
| `id` | string | Stable key. Referenced by boards, workflow `capability` steps and presets, so renaming one breaks those references — add a new document instead. |
| `name` | string | What an operator reads on the button or card. |
| `category` | enum | `validation`, `performance`, `hardware`, `networking`, `maintenance`, `setup`. Drives the badge colour and the grouping on the Tools screen. |
| `status` | enum | `ga`, `preview`, `future`, `deprecated`. A `future` tool can be described before it exists. |
| `source` | string | `seed` for the built-in set, anything else for an operator's. Seeding never clobbers a document whose source is not `seed`, which is what makes a local edit survive an upgrade. |
| `description` | string | Shown in the info dialog. |
| `ai_context` | string | Extra guidance for the assistant — an example invocation, a caveat. Not shown to the operator. |

## What runs

| Field | Type | Purpose |
|---|---|---|
| `script` | basename | **A filename, never a path.** Resolved at launch through the script tree: the project's own copy, then `custom/`, then the platform layer, then the shared defaults. See [How a script path is chosen](../../explanation/how-a-script-path-is-chosen.md). |
| `env_key` | string | An override name such as `RACK_SYSINFO_SCRIPT`. When a site sets it, that absolute path wins over the whole search order. |
| `params` | list | Per-parameter `key`, `label`, `description`, `default`, `required`. A workflow step that omits a required parameter fails rather than running with a guess. |

`script` must be a plain basename: a value carrying `..` or a shell metacharacter is refused on
write *and* re-checked at launch, because `catalog.json` can be edited by hand on the box and
never passes through the API.

A **compiled binary is not a script.** A basename only resolves inside the script tree, so a
`nccl-tests` build in a workspace will not be found there. Wrap it in a script — which is also
where the fabric environment belongs — or point `env_key` at an absolute path.

## Where it appears

| Field | Type | Purpose |
|---|---|---|
| `surfaces` | list | UI locations. The seeded set uses `rackDetail.validation`, `rackDetail.hardware`, `rackDetail.networking`, `rackDetail.services`, `multirack`, `tools`, `analytics`, `clusterDashboard`, `parserDialog`, `infraIbNvlink`. |
| `order` | int | Sort position within a surface. |
| `subtitle` | string | One ellipsised line under the name — a version, a target, a scope. |
| `button_label` | string | Overrides the default verb on the button. |
| `icon` / `image` | string | The card's iconography. |
| `destructive` | bool | Renders the action in the danger colour and makes confirmation the default. |

A surface is a *suggestion*, not a permission: "Add action" on a board offers every tool the
cluster can run, because a tool seeded onto the multirack board is still a legitimate thing to
want on a rack board.

## Which clusters are offered it

| Field | Type | Purpose |
|---|---|---|
| `compatible.match` | `all` \| `none` \| *(empty)* | `all` offers it everywhere; empty means the same. |
| `compatible.capabilities` | list | Offered where the platform declares all of these — `nvlink`, `nvswitch`, `imex`, `mnnvl`, `roce`. |
| `compatible.platform_ids` | list | Offered only for these platform ids. |

**Compatibility decides whether a tool is offered, never which copy of its script runs.** A tool
marked compatible with GB200 and GB300 still resolves through the four script layers; a
per-platform *implementation* comes from the platform layer, which bootstrap copied into the
project's directory. Nothing branches on platform at run time.

## How it executes

| Field | Type | Purpose |
|---|---|---|
| `execution.mode` | enum | `async` (a tracked job), `sync` (a request that returns the result), `stream` (output as it arrives). |
| `execution.target` | enum | `rack`, `multirack`, `cluster`, `node` — what the operator selects before launching. |

## How the button behaves

`launch` binds the card to a real endpoint, which is what lets a catalog edit add a working
button rather than a decorative one.

| Field | Type | Purpose |
|---|---|---|
| `launch.kind` | enum | `validation-preset`, `async-job`, `stream`, `dialog`, `none`. |
| `launch.endpoint` | string | The API path for `async-job` and `stream`. |
| `launch.preset_id` | string | The preset for `validation-preset`. |
| `launch.dialog` | string | Which dialog `dialog` opens. |
| `launch.scopes` | list | Which target scopes the button offers. |
| `launch.body` | object | Fixed body fields sent with the launch. |
| `launch.confirm` | bool | Require confirmation first. |
| `launch.min_nodes` / `max_nodes` | int | Refuse a selection outside this range — the guard that stops a 2-node run of a test that needs a full rack. |

## Adding one

The Tool Registry screen writes these documents; see
[How to add your own script and register it as a tool](../../how-to/add-your-own-script.md) for
the procedure, including where the file has to live on the head node.

Two things the registry gates rather than trusts: `script` is re-validated at launch, and
importing a shared board requires the `catalog.tool:create` or `catalog.tool:update` permission
the underlying write would have needed on its own — so a board file cannot widen what its
importer may do.
