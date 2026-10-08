# Published settings for this scripts copy

This directory ships empty on purpose. Rack Status **publishes** settings into it -- facts that
live in its store and that a launcher, or a job already running on a node, has to read:

| File | What it is | Read by |
|---|---|---|
| `image-catalog.tsv` / `image-catalog.json` | The cluster's P2P image catalog: each image's filename, byte size and SHA256 pin. | `lib/node_images.inc.sh` checks a node's image against it before a launch. |

It follows the scripts it sits beside. A project runs from the most specific script layer that
exists -- `project-<id>/`, else `<platform-id>/`, else `multi-platform/` -- and Rack Status writes
into that layer's `config/`. A project that is later bootstrapped gets this directory copied into
its own `project-<id>/`, and the next publish fills it there. Scripts read `config/` relative to
their own directory (`lib/node_config.inc.sh`), so no path is configured twice.

Published on every catalog save, project script bootstrap and script deploy, or on demand from
**P2P -> Catalog -> Publish to scripts**. These files are a projection of Rack Status's store:
edit the catalog there, never here -- the next publish overwrites them.

This is script execution's own copy. The P2P control plane keeps its own manifest and receipts to
know whether *it* is in sync; scripts never read those.
