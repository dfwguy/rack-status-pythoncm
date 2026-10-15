---
diataxis: explanation
description: Which of the four script layers runs, in what order, and which of them anybody may write.
---

# How a script path is chosen

Registering a Tool records a **basename** — `sysinfo.sh`, `nccl_allreduce.sbatch` — never a
path. This page is the decision behind that: which directory the app looks in, in what order,
and which of those directories anybody is allowed to write.

It exists because the ambiguity was real. Before the layered tree, two resolvers disagreed:
`paths._scripts_dir()` honoured the topology `scripts` role while `workflow_engine` read
`RACK_REMOTE_BASE_PATH` directly, so on a deployment that declared a scripts role the API and a
workflow step ran *different copies of the same script* and nothing reported it.

## The layers

One server runs several customers' projects. A script resolves through four directories, most
specific first:

| Order | Directory | Who owns it | Writable by the app |
|---|---|---|---|
| 1 | `{base}/scripts/project-<project-id>/` | one project | yes |
| 2 | `{base}/scripts/custom/` | this deployment's operators | yes |
| 3 | `{base}/scripts/<platform-id>/` | the product (shipped) | no |
| 4 | `{base}/scripts/multi-platform/` | the product (shipped) | no |

The first directory that contains the basename wins. `backend/script_tree.py` is the only module
that answers this; asking `RACK_REMOTE_BASE_PATH` yourself is how the two resolvers came to
disagree.

An explicit env override still beats all four. A site that sets
`RACK_SYSINFO_SCRIPT=/opt/site/sysinfo.sh` gets exactly that path, layering or not.

## Where to put a script

**A script that runs anywhere → `multi-platform/`.** This is the shipped default set, and it is
what a project's copy is composed from.

**A script that runs on exactly one platform → `<platform-id>/`.** `gb200-nvl72`, `gb300-nvl72`,
and so on, named after the catalog Platform id. The whole file is replaced for that platform, so
reach for this only when the *script* differs — a different scheduler invocation, a different
tool, a different sequence. When a threshold, an image tag or a binary name differs, a
`tool_param_defaults` entry on the Platform record reaches the script as an environment variable
and keeps one file. `CUDA_MAJ` works that way: one CUDA major per platform, read at run time
to pick the workspace toolkit, with one script serving them all. Better still is no per-platform value at all:
ClusterKit used to pick a fixed per-platform CPU/HCA mapper (`core_to_hca_dgxgb200.sh`/`300.sh`)
and now reads each rail's GPU, core and NUMA node from the node itself (`nvis_fabric_rail_map`).

**A script you wrote for this deployment → `custom/`.** Shared across every project on the
server, and never touched by a deploy or by bootstrapping a project.

**A change to one project's copy → edit it in `project-<id>/`.** That directory is a full copy,
composed at bootstrap from the default set with the platform layer over the top, and nothing
overwrites a file you have changed there.

## Why `custom/` exists

Two rules that are each correct were in direct conflict.

The shipped layers must not be edited in place: they are shared by every project on the server,
so a fix for one customer silently changes another customer's run. That is the whole reason a
project gets its own copy.

But registering a Tool only records a basename — somebody has to put the file on the head node.
Before `custom/`, the only writable directory was a project's, which would make a genuinely
cross-platform script the property of whichever project happened to be active when it was added.

So `custom/` is operator-owned and deployment-wide. It sits *above* the shipped layers, so a
script there shadows a shipped one of the same name without editing it — the same relationship a
project directory has, one scope wider.

**To change what ships with the app, open a merge request.** A script that belongs in the
product should arrive in `multi-platform/` (or a platform layer) through review, and then every
deployment gets it on the next `bin/deploy-scripts.sh`. `custom/` is deliberately not a
substitute for that: a script only in `custom/` exists on one head node, is invisible to every
other site, and nobody reviews it.

## What immutability means here, precisely

It is a property of the **layer**, not a flag on a record. `script_tree.is_writable()` answers
from the path alone and there is nothing to toggle — a `protected` field on a Platform document
could be cleared, and then the shared `gb300-nvl72/` scripts would be editable for every project
on the box, which is the outcome being prevented. Platform *records* stay fully editable, since
tuning `tool_param_defaults` per site is routine.

Two clarifications that follow from "the layer, not the file":

- **The shipped layers are written by a deploy, and only by a deploy.** `bin/deploy-scripts.sh`
  is one; `POST /api/scripts/deploy`, behind the setup wizard's Scripts step and the Projects
  screen's "Deploy scripts to head", is the other. The rule this protects was never "the app's
  process must not write that path" — it is that updating the product's scripts is a deliberate
  act, never a side effect of something else. A checkbox an operator ticks on a setup screen
  meets that; a launch quietly refreshing a layer on its way past would not.

  This used to read "*the app* never writes them", which described the implementation of the
  day rather than the principle, and the gap showed: an operator setting up an appliance has a
  browser and no repo checkout, so the only route to a usable head node was a shell script they
  had no way to run. Two properties keep the weaker rule sound — the deploy is **additive**
  (`custom/` and `project-*/` are not in the archive, so extraction cannot touch them, and there
  is no delete pass), and it is **explicit** (its own endpoint, its own action, its own audit
  line, never invoked by a launch).
- **The legacy flat `{base}/bin/scripts` counts as shipped.** On a deployment that has not been
  migrated it is shared by every project, so a write there has the widest blast radius of all.
  Creating the tree is what makes a writable layer available.

## What the compatibility list does *not* do

A Tool's platform compatibility — `all`, by capability, or specific platform ids — decides
**whether the tool is offered** for a cluster. It never affects which directory the script comes
from. A tool marked compatible with GB200 and GB300 still resolves through the four layers
above; the *implementation* difference comes from the platform layer, which bootstrap copied into
the project directory. Nothing branches on platform at run time.

## One project, one directory

There is one script directory per **project**, not per cluster. A project whose clusters span two
platforms gets whichever platform layer was bootstrapped first, and a later bootstrap will not
overwrite it — by design, since that is the rule protecting operator edits. Where a project is
one platform, which is every deployment today, this never arises. If mixed-platform projects
become real, the directory needs the cluster id in its name.

## Related

- [How to add your own script and register it as a tool](../how-to/add-your-own-script.md)
  — the task version of this page.
- `bin/bootstrap-project-scripts.sh` — composes a project's directory from the shipped layers.
- `docs/EXECUTION_ARCHITECTURE.md` — which *host* runs the script, as opposed to which copy.
