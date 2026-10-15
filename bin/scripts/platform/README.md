# Platform script layers

A script in here **replaces** its namesake from the shared default set (`bin/scripts/`) for one
platform only. `bin/deploy-scripts.sh --tree` copies each `platform/<platform-id>/` to
`<base>/scripts/<platform-id>/`, and `bootstrap-project-scripts.sh` composes a project's
directory as *defaults, then the platform layer over the top*.

`<platform-id>` is a catalog Platform id — `gb200-nvl72`, `gb300-nvl72`, `b200-hgx-intel`,
`b300-hgx`, `vr200-nvl`, and so on. `python backend/cli.py check` will not tell you these; the
Platform catalog view will, and so will `GET /api/catalog/platforms`.

## When *not* to put a script here

Most platform differences do not need a separate file, and duplicating a script to change two
lines is how the copies drift apart. Two cheaper mechanisms already exist:

- **A platform parameter.** `tool_param_defaults` on the Platform record reaches the script as
  an environment variable. `CUDA_MAJ` works this way: one CUDA major per platform, read at
  run time to pick the workspace toolkit, with one script serving them all. Nothing had to move.
- **A capability test in the script.** `lib/fabric.inc.sh` and friends already branch on what
  the node reports rather than on which platform it is supposed to be, which keeps working when
  a site turns out not to match its own SKU sheet.

Put a file here when the *whole* script differs — a different scheduler invocation, a different
tool, a different sequence — not when a threshold or an image tag differs.

## The directory is empty on purpose

Nothing needs a platform layer today. It is created so the convention is discoverable, and so
`deploy-scripts.sh --tree` has something to walk.
