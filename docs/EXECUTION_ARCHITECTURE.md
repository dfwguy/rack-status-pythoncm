# NVIS Rack Status — Execution Architecture (local vs remote)

This document explains **how NVIS runs cluster commands and shell scripts**, and the
**design patterns** that let the *same* code and the *same* `bin/scripts/` run correctly in
three very different places:

1. **On the cluster head node** (the classic deployment: NVIS installed under
   `…/nvis-rack-status`, `RACK_EXEC_MODE=local`).
2. **Inside a Docker container on a cluster node** (still `local`-ish — same host, no SSH).
3. **On a developer laptop pointed at a remote cluster** (`RACK_EXEC_MODE=ssh`, possibly
   from **macOS**).

Most bugs we hit during the Firmus/MEL01 and Boston Markley deployments come from a single
root cause: **a path, a hostname, or a transport that is correct in one of those contexts
is silently wrong in another.** The patterns below exist to make execution
*context-independent*.

> TL;DR design rule: **Never assume the script runs where the dashboard process runs.**
> Resolve paths, hostnames, and transports against the *target* filesystem/cluster, not the
> local one.

See also: [`docs/ENVIRONMENT.md`](ENVIRONMENT.md) (every env var) and
[`.cursor/rules/cluster-naming-conventions.mdc`](../.cursor/rules/cluster-naming-conventions.mdc)
(rack label vs real node hostname).

---

## 1. The execution layer: `host_exec`

All host commands route through **`backend/host_exec.py`** — never raw `subprocess.run()`
for anything that touches the cluster. The layer has two responsibilities:

| Responsibility | Why it matters |
|----------------|----------------|
| **Mechanism** | The layer holds a composed connection and applies the right mechanism — direct subprocess, SSH, or a chain of hops. The caller writes the command **once** and never branches on where it runs. (`RACK_EXEC_MODE` is deprecated and defaults to `ssh`; there is no `is_remote()` predicate to test.) |
| **Environment forwarding** | A local subprocess inherits the daemon's env for free. An **SSH** session does **not** — so `host_exec` re-exports a whitelist of variables on the remote side. |
| **Filesystem** | The same connection answers `stat_dir` / `read_text` / `write_bytes` / `is_dir`, so a path is always evaluated against the host that owns it rather than the one the process happens to run on. |

### 1.1 Environment forwarding whitelist

```python
# backend/transport/factory.py  (moved from host_exec.py so transport is self-contained)
FORWARD_ENV_PREFIXES = ("RACK_", "NEMO_", "VALIDATION_", "SQUEUE_", "NVIS_", "PDSH_", "NGC_", "LLMB_")
FORWARD_ENV_NAMES = (
    "CUDA_MAJ", "GPU_MODEL_FLAG", "MNNVL_TOPOLOGY", "CORE_TO_HCA_SCRIPT",
    # Fabric wireup overrides (lib/fabric.inc.sh)
    "GPUS_PER_NODE", "NCCL_IB_HCA", "NCCL_SOCKET_IFNAME", "UCX_NET_DEVICES", "UCX_TLS",
    "OMPI_MCA_coll_ucc_enable", "OMPI_MCA_btl_tcp_if_include", "OMPI_MCA_oob_tcp_if_include",
)
```

A **per-call** value that is not in the allowlist goes through `env_extra`, which is
exported on the far side regardless of prefix (this is what makes the launch
dialog's free-form "Extra environment variables" work for `NCCL_*`). The `env`
argument is a local-subprocess parameter and is **dropped** when the target is
remote.

**Pattern:** any knob a remote script needs must match a forwarded prefix/name, otherwise it
exists on the dashboard host but vanishes over SSH. This is why all our tunables are prefixed
`RACK_*` / `NVIS_*` etc. — the prefix *is* the forwarding contract. (`PDSH_*` is on the list
specifically so the transport fix in §3 can also be applied globally via the environment.)

### 1.2 Hybrid SSH profiles: `control` vs `script`

Some clusters split capability across two hosts. Boston Markley is the canonical example:

| Plane | Host / user | Can do | Cannot do |
|-------|-------------|--------|-----------|
| **control** (`RACK_SSH_*`) | `cmods@head` | `cmsh`, `module load slurm`, `sinfo`/`squeue` | **no root**, no `cmsh` is missing on slogin |
| **script** (`RACK_SCRIPT_SSH_*`) | `root@slogin` | root, `pdsh`/`pdcp` fan-out to compute nodes, `ipmitool` | `cmsh` not present |

`host_exec` selects the target with a `profile` argument (`role=` is the newer
spelling of the same thing — see §7):

```python
host_exec.run_on_host(cmd, env=_subprocess_env(), profile="script")  # root@slogin
host_exec.run_on_host(cmd, env=_subprocess_env())                    # control (default)
```

**Pattern:** *control-plane* reads (cmsh/slurm) and *privileged node-fanout* (sysinfo,
hw-check, NVSwitch audit, resets) are different trust domains. Tools pick the profile that
matches what they actually need root/`pdsh` for. On single-host clusters `RACK_SCRIPT_SSH_*`
is simply unset and both profiles collapse to the same target.

---

## 2. Node targets: live inventory, not name templates

NVIS knows two kinds of identifiers and **they are not interchangeable**:

- **Rack label** — a UI/grouping id like `bos01-b05` (from the `NVIS_RACK_ID` convention).
- **Real node hostname** — what `pdsh`/`ssh` must actually connect to, e.g.
  `bos01-b05-13-s1-dgx-05-c03`.

Early code expanded a `NVIS_CT_ID` *template* (`bos01-b05-[01-18]`) to build node targets.
That produced **hostnames that do not resolve** on sites whose real names don't match the
template → `ssh exited with exit code 255`.

**Pattern (template-free targeting):** derive node targets from **live Slurm inventory**
(`sinfo -N`) by prefix-matching the rack id, and only fall back to the template when an
explicit override is given or inventory is unavailable.

```python
# backend/app.py
validation_target_for_rack(rack, override)      # inventory first, template fallback
_rack_node_hostnames_from_inventory(rack)       # real NodeNames matched to the rack id
# kill switch: RACK_TARGET_FROM_INVENTORY=0 forces the template path
```

This is why `hw-check` and `sysinfo` now target real `…-s1-dgx-…-cNN` hostnames regardless
of the customer's naming scheme.

---

## 3. The `pdsh` transport pitfall (rsh vs ssh)

This is the finding that motivated this document.

### Symptom

`hw-check` on `bos01-b05` returned, for **every** node:

```
pdsh@bos01-a09-13-s1-mgmt-01: bos01-b05-13-s1-dgx-05-c03: connect: Connection refused
pdcp@bos01-a09-13-s1-mgmt-01: bos01-b05-13-s1-dgx-05-c03: connect: Connection refused
```

The nodes were healthy (`sinfo` showed them all `idle`), root SSH worked, and the *exact same*
node list ran fine under `sysinfo`. So it was neither node state, nor trust, nor naming.

### Root cause

`pdsh` was compiled with a **default rcmd module of `rsh`**, and `PDSH_RCMD_TYPE` was unset:

```text
$ pdsh -V
rcmd modules: ssh exec
(default: rsh)            # <-- rsh isn't even a usable transport here
$ echo $PDSH_RCMD_TYPE
<unset>
```

With no override, `pdsh`/`pdcp` tried **rsh** (port 514). No `rshd` is running → the port
actively refuses the connection → `connect: Connection refused` on all nodes.

The asymmetry was the giveaway:

| Script | Forces ssh? | Failure signature |
|--------|-------------|-------------------|
| `sysinfo.sh` | **yes** (`export PDSH_RCMD_TYPE=ssh` + `-R ssh`) | `ssh exited with exit code 255` (ssh transport, auth/timeout) |
| `hw_check_pdsh.sh` (old) | **no** | `connect: Connection refused` (rsh transport) |

Different error strings, *same* nodes — that's a **transport** difference, not a reachability
difference.

### Fix — force ssh transport, two complementary layers

**Layer A — make the script self-contained** (matches `sysinfo.sh`, works in every context):

```bash
# top of the script
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh
# and pass -R ssh explicitly on every fan-out
pdsh -R ssh -w "$NODES" …
pdcp -R ssh -w "$NODES" …
```

Applied to `hw_check_pdsh.sh`, `run-nemo-xrack.sh`, `run-llama.sh`, `run_singlerack.sh`
(commit `fc2f178`). `nvswitches_rack_audit.sh` already used `-R ssh`.

**Layer B — environment safety net.** Because `PDSH_` is in `_FORWARD_ENV_PREFIXES`, setting
`PDSH_RCMD_TYPE=ssh` (and `PDSH_SSH_ARGS_APPEND`) on the NVIS service/launcher forwards it to
**every** remote session, so any *future* pdsh script inherits the correct transport even if
someone forgets the in-script export.

### Validation

After the fix, the same `hw-check` returned real diagnostics instead of a refused storm:

```
bos01-b05-17-s1-dgx-05-c07: ERROR: IB HCA number is not 4
bos01-b05-32-s1-dgx-05-c13: WARN: mlx5_4:link_downed=4 …
```

### Design rule

> **`pdsh`/`pdcp` must never rely on the compiled default rcmd.** Always force `ssh`
> (`PDSH_RCMD_TYPE=ssh` + `-R ssh`) and append non-interactive SSH options. The compiled
> default is site-dependent and frequently `rsh`, which is dead on modern clusters.

---

## 4. The path-resolution pitfall (resolving remote paths on the local host)

### Symptom

Running NVIS locally on a Mac (`run-local-boston.sh`), the **Download NEMO container**
destination defaulted to `/System/Volumes/Data/home/cmods` instead of `/home/cmods`.

### Root cause

The default was derived from `NEMO_SQSH_SRC=/home/cmods/nemo-…sqsh` and the code called
`Path(...).resolve()`. On **macOS**, `/home` is a synthetic **firmlink** to
`/System/Volumes/Data/home`, so `.resolve()` rewrote the path. But in `ssh` mode that path
belongs to the **remote Linux cluster** — resolving it against the *local* filesystem is
simply the wrong filesystem.

### Fix

Use a **pure** path (no filesystem access) so the remote path is preserved verbatim:

```python
# backend/app.py — _tools_download_destination_default()
p = PurePosixPath(raw)              # NOT Path(raw).resolve()
return str(p.parent) if p.suffix.lower() == ".sqsh" else str(p)
```

### Design rule

> **Never call `.resolve()` / `os.path.realpath` / `expanduser` on a path that targets the
> remote cluster.** Those touch the *local* filesystem (symlinks, firmlinks, `~`). Treat
> remote paths as opaque POSIX strings (`PurePosixPath`); only resolve paths that are truly
> local to the process.

---

## 5. Defaults must follow the cluster, not be hardcoded

Different clusters use different operator homes: Firmus = `cmsupport`, Boston = `cmods`.
Hardcoding `/home/cmsupport/...` as a UI default or backend fallback leaks one site's
convention onto another.

**Pattern:** UI/field defaults come from **env-driven backend values**, surfaced once in the
cluster-overview payload and consumed by the frontend:

| Concern | Env var | Backend helper | Surfaced as |
|---------|---------|----------------|-------------|
| Toolchain workspace (HPC-X / CUDA / nvloom) | `NVIS_WORKSPACE_DIR` | `_nvis_workspace_dir_effective()` | `tools_workspace_default` |
| NEMO download dir | `NEMO_SQSH_SRC` | `_tools_download_destination_default()` | `tools_download_destination_default` |
| Reports dir | `RACK_BUG_REPORT_REMOTE_DIR` | reports base helpers | (reads at request time) |

The frontend seeds inputs from these (falling back to `localStorage`), so the same build shows
`/home/cmods/workspace` on Boston and `/home/cmsupport/workspace` on Firmus with **no code
change** — only the env differs.

### Design rule

> **Site-specific paths belong in environment, not in source.** Backend helpers compute the
> effective value; the frontend reads it from the API. Hardcoded `/home/<user>/...` literals
> are a bug, not a default.

---

## 6. Fan-out at scale: the composable connection chain and the gateway sshd ceiling

The execution layer has evolved from `host_exec` + in-script `pdsh` into a composable
**`transport.Connection`** abstraction (`backend/transport/`). A connection's target can be
a host **or another connection**, so the path to a compute node is just connections chained:

```
app  ──►  head (control)  ──►  node        # control.hop(node)
```

`Connection.hop(host, sudo=…)` runs `ssh host <cmd>` **through** the base connection (the
node leg executes on the head, using the head's credentials), and **`transport.fanout()`**
scales that across N nodes — the modern, Perl-free replacement for `pdsh`/`dshbak`
(`bin/scripts/nvis_fanout.py` is the standalone equivalent). See
[`docs/WORKFLOW_COMPOSITION.md`](WORKFLOW_COMPOSITION.md).

### Symptom

Fanning a workflow check across a full 18-node rack, a few nodes intermittently failed:

```
--- errors ---
bos01-b05-16-…: Connection closed by 10.29.136.8 port 22
bos01-b05-33-…: kex_exchange_identification: read: Connection reset by peer
--- fanout: 18 node(s), 16 ok ---
```

### Root cause — everything funnels through **one** sshd

`10.29.136.8` is not a node; it's the **head the app SSHes through**. Because every node hop
is `app → head → node`, all N hops open sessions against the **head's single sshd**, which
enforces two ceilings:

| Topology | Ceiling hit | Server knob (default) |
|----------|-------------|-----------------------|
| N separate SSH connections | connection **MaxStartups** | `10:30:100` |
| N sessions over one multiplexed master | **MaxSessions** per connection | `10` |

So concurrency ≳10 through a single gateway trips one limit or the other — **SSH
ControlMaster multiplexing alone does not fix it** (it just trades MaxStartups for
MaxSessions). This is *not* a node/reachability problem (contrast §2/§3, which are transport
and naming).

### Fix — multiplex **+** bounded concurrency **+** retry

Three knobs together (all validated on Boston: 14/18 with resets → **18/18, 0 errors, ~6s**):

1. **ControlMaster multiplexing** on the app→head connection (`SSHConfig.multiplex`, default
   on; opt-out `RACK_SSH_MULTIPLEX=0`). Concurrent hops reuse **one** TCP + handshake
   (`ControlMaster=auto` + `%C`-hashed `ControlPath` + `ControlPersist`), plus a serial
   *warm-up* that establishes the master before the parallel wave (avoids the create-race).
2. **Bounded concurrency** — `transport.fanout` `max_workers` defaults to **8**, under the
   typical sshd ceiling (~10). All concurrent sessions ride one master; larger racks run in
   waves that reuse the persistent master (no re-auth). Tunable via `RACK_FANOUT_MAX_WORKERS`.
3. **Retry transient handshake errors** — `kex_exchange_identification`, connection
   reset/closed, `Session open refused`, etc. are retried with a short backoff (real command
   failures are not).

### Scalability notes (important)

> **Note 1 — raising throughput.** `RACK_FANOUT_MAX_WORKERS` (default 8) is deliberately
> conservative for a single gateway sshd. If the head's `sshd_config` allows more
> (`MaxStartups` / `MaxSessions` raised), increase it for faster large-rack fan-out. Keep it
> **≤ the head's `MaxSessions`** when multiplexing, or you reintroduce the reset storm.

> **Note 2 — the single-gateway ceiling is fundamental to app-tier fan-out.** Routing every
> node hop through one head sshd (`app → head → node ×N`) will always be capped by that one
> daemon. The scalable long-term model (how `pdsh` avoids this) is to **run the fan-out *on*
> the head** so the N connections spread across N **distinct node** sshds — no single-gateway
> ceiling. In the Connection model this is a "fan-out at the gateway" mode: one `app → head`
> connection whose single remote invocation fans `head → node ×N` in parallel and returns the
> coalesced result. It's an optional, not-yet-default execution mode (bounded app-tier
> concurrency covers rack scale today); reach for it at cluster scale.

### 6.1 The stale ControlMaster — multiplexing's own failure mode

Multiplexing is **load-bearing**, not an optimization: with it working, every poll and fan-out
rides one authenticated TCP connection to the head. When the master goes bad the app keeps
working but degrades in a way that reads like a cluster or data problem, so it is worth
recognising on sight. Observed on Nscale TX 2026-08-05.

#### Symptom

The dashboard turns sluggish and shows stale or partly-missing data — not errors. The tell is in
the container log / any hop's stderr:

```
mux_client_request_session: session request failed: Session open refused by peer
ControlSocket /root/.ssh/nvis-mux-<hash> already exists, disabling multiplexing
```

**"disabling multiplexing" is a per-attempt client fallback, not a configuration change.** Nobody
turned multiplexing off; OpenSSH found the socket unusable and opened a fresh, unmultiplexed
connection *for that one command*. `RACK_SSH_MULTIPLEX` is still `1`.

#### Root cause — the master outlives its usefulness, and nothing cleans it up

1. `ControlPersist` (60s) only starts counting when the master goes **idle**. Continuous status
   polling means it never does, so one master can live for **days** (ours was ~24h old, still
   reporting `Master running (pid=16)`).
2. Concurrent API polls open **channels** on that single connection. The head caps channels per
   connection with `MaxSessions` — **default 10, and typically not set in `sshd_config`**
   (Nscale TX sets only `MaxStartups 10:30:60`, so the invisible default applies).
3. Hit that ceiling, or wedge a channel on slow I/O, and the peer starts refusing new sessions.
   A **full filesystem makes this far more likely** — on Nscale TX `/` was at 94%, so commands
   touching `/home/cmods` stalled and channels piled up instead of closing.
4. The client falls back for that command but **leaves the dead socket on disk**. Every later hop
   rediscovers the same corpse, pays a failed handshake, then reconnects from scratch. The
   degradation is permanent until someone removes the socket.

The retry logic in §6 does not save you here: this is not a transient reset that succeeds on
attempt two, it is a stale file that keeps failing identically.

#### Diagnose

Socket path is `<mux dir>/nvis-mux-%C`, where the dir is `~/.ssh` when writable (so `/root/.ssh`
in the container) else `$TMPDIR` — see `_mux_control_dir()` in `backend/transport/ssh.py`.

```bash
docker exec nvis-rack-status-v3 bash -lc '
  ls -la /root/.ssh/nvis-mux-*
  for s in /root/.ssh/nvis-mux-*; do ssh -O check -o ControlPath="$s" <user>@<head>; done'
```

`Master running (pid=…)` is **not** proof of health — a half-dead master reports exactly that.
The real test is whether a second session can attach:

```bash
ssh -o ControlPath="$s" -o ControlMaster=no <user>@<head> 'echo SECOND_SESSION_OK'
```

#### Fix (recovery)

```bash
docker exec nvis-rack-status-v3 bash -lc '
  for s in /root/.ssh/nvis-mux-*; do ssh -O exit -o ControlPath="$s" <user>@<head>; done
  rm -f /root/.ssh/nvis-mux-*'
```

Or `docker restart <container>` — the socket lives in the container filesystem, so a restart
clears it. The next hop transparently establishes a fresh master; no config change is needed.

#### Prevention

- Raise the head's **`MaxSessions`** explicitly rather than relying on the default 10, and keep
  `RACK_FANOUT_MAX_WORKERS` ≤ that value (§6, Note 1).
- **Treat head-node disk pressure as an SSH-reliability issue**, not just a capacity one. Stalled
  commands hold channels open and are what pushes a long-lived master into the ceiling.
- When triaging "the dashboard shows odd data", check the mux socket **before** suspecting the
  data path. A refused-session master and a genuinely broken probe look identical in the UI.

### Design rule

> **Fan-out through a shared gateway is bounded by that gateway's sshd.** Multiplex to cut
> handshakes, cap concurrency at/under the sshd session limit, and retry transient resets.
> To scale past one gateway, move the fan-out to where it's one hop from the targets (on the
> head), so load spreads across the targets' own daemons.

> **A long-lived ControlMaster is shared mutable state, so treat it as something that can go
> bad.** `Master running` is not health; the only proof is attaching a second session. When it
> refuses, SSH degrades silently and does not self-heal — clear the socket (§6.1).

---

## 7. Declaring where things are: endpoints and roles

§6 gives the *mechanism* for reaching a host through another one. This section is
the **configuration** that makes it usable, and it is what lets one build run on a
laptop, in a container, or on the head node without a code change.

### The gap it closes

The flat `RACK_SSH_*` contract can express exactly one hop to one host, and
`RACK_REMOTE_BASE_PATH` points scripts *and* logs at one tree on that host. So
"scripts here, logs there, BCM somewhere else", or "reach the head through a
bastion", were unsayable — `HopExecutor` existed but nothing could ask for it.
Worse, locations were bound to *paths only*, never to a connection, so any call
site that wanted a file had to assume it was local.

### The model

Two lists on the cluster document (`connection.endpoints`, `connection.roles`):

| Concept | Answers | Fields |
|---------|---------|--------|
| **Endpoint** | *how do I get there?* | `id`, `type` (`local`/`ssh`), `host`, `user`, `key`, `port`, `via`, `sudo`, `opts` |
| **Role** | *what does this call site want?* | `id`, `endpoint`, `root`, `sudo` |

`via` names another endpoint, and each leg runs **on** its predecessor using that
host's credentials. N hops is N links — no special two-hop code path:

```
app ──► bastion ──► head ──► node        # three endpoints, each via the last
```

Roles are the indirection that matters. `control`, `scripts`, `logs` and `bcm` are
seeded because the shipped call sites ask for them, but the set is **open**: a site
can invent any role name and bind it. Code names the role and the deployment
decides which host that is:

```python
host_exec.run_on_host(cmd, cwd=_scripts_dir(), role="scripts")
host_exec.stat_dir(log_dir, patterns=("*.txt",), role="logs")
host_exec.read_text(p, role="logs")
```

An unknown role falls back to `control`, so a call site written role-first still
works on a deployment that has not split that role out.

> **`script` vs `scripts`.** These read alike and are *different axes*. `script` is
> the privileged trust domain of §1.2 (root/`pdsh`, `RACK_SCRIPT_SSH_*`); `scripts`
> is a *location* (where `bin/scripts` lives). `script` therefore falls back to
> `control`, never to `scripts` — aliasing them would route privileged node
> operations to whichever host merely stores the files. Declare a `script` role
> explicitly when the privileged route is a separate host.

### Where it lives, and back-compat

The topology sits on the catalog's `Cluster` document, so it is per-cluster
(switching the active cluster switches the whole route), runtime-editable through
the store the Projects UI already writes, and covered by the existing overlay
invalidation. `GET/PUT /api/catalog/topology` exposes it; `GET` also returns the
**resolved** view (per role: endpoint, hop path, root), which is the useful thing
when asking "which host did that command actually run on?".

Deployments that declare nothing are untouched: the original single-target
composition is built exactly as before, with role roots derived from
`RACK_REMOTE_BASE_PATH` so call sites read the same either way. `transport/
topology.py` takes plain dicts and imports no catalog or Flask, so the resolver
stays standalone and unit-testable (`backend/tests/test_topology.py`).

### Design rule

> **A call site names a *role*, never a host, a transport, or a bare path.** Where
> that role lives — same box, one hop, or four — is deployment configuration. If a
> fix needs to know where something is, the fix is a role binding, not a branch.

---

## 8. Checklist for adding a new cluster tool/script

When you add a script that touches compute nodes, verify it survives all three contexts:

- [ ] Invoked via `host_exec.run_on_host(...)` / `popen_on_host(...)` — **never** raw
      `subprocess` for cluster work. This includes anything passing `cwd`: a cluster
      directory does not exist in the container.
- [ ] Correct `role`/`profile` (`script` if it needs root or `pdsh` fan-out;
      `scripts`/`logs` for work that must happen where those trees are; `control` for
      cmsh/slurm reads). See §7.
- [ ] Files touched through `host_exec` (`stat_dir`, `read_text`, `write_bytes`,
      `is_dir`, `makedirs`) — **never** `pathlib`/`shutil`, which answer for the
      local filesystem.
- [ ] Per-call env passed as `env_extra` (crosses the SSH boundary), not `env`
      (local-subprocess only, silently dropped when remote).
- [ ] Node targets come from **live inventory** (`validation_target_for_rack` /
      `_rack_node_hostnames_from_inventory`), not a name template.
- [ ] Any `pdsh`/`pdcp` forces **ssh** transport (`PDSH_RCMD_TYPE=ssh` + `-R ssh` +
      `PDSH_SSH_ARGS_APPEND`).
- [ ] Tunables use a forwarded prefix (`RACK_*`, `NVIS_*`, `NEMO_*`, `VALIDATION_*`,
      `SQUEUE_*`, `PDSH_*`) so they cross the SSH boundary.
- [ ] Remote paths are treated as opaque POSIX strings — no local `.resolve()`/`expanduser`.
- [ ] Any user-facing path default is env-driven, not a hardcoded `/home/<user>/...`.
- [ ] If it fans out to many nodes, it goes through `transport.fanout` (or a bounded
      `Connection.hop` loop) — **not** an unbounded parallel SSH storm. Respect the gateway
      sshd ceiling: multiplex on, concurrency ≤ `MaxSessions` (`RACK_FANOUT_MAX_WORKERS`). See §6.
