---
diataxis: how-to
description: Open a shell on the head node or a tray from the browser, and know what the session is and is not.
---

# How to use the terminal

Greenlight can give you an interactive shell on the hosts it already reaches, in the browser,
without a separate SSH client or a second tunnel. It is the same connection the actions use.

## Open a shell on the head node

Go to **Tools → Terminal** (`/tools/terminal`). The session opens on the cluster's control
endpoint — there is no host to pick, because it is the connection the app already holds.

Confirm you are where you expect before running anything:

```bash
hostname; whoami
```

On a split deployment such as Boston this is the **control** endpoint (`cmods` on the head), not
the scripts endpoint where `pdsh` and `sbatch` run as root.

## Open a shell on a compute node

Open the rack, then the node's row, and choose the terminal action there. The session is hopped
through the head node the same way any other node-scoped action is.

Verify the hop landed on the tray you picked, because a wrong naming template produces a session
on a host that merely resolves:

```bash
hostname; nvidia-smi -L | head -2
```

If that reports `Name or service not known`, stop: the cluster's node template is wrong, and
every node-scoped action is targeting hosts that do not exist. See
[Geometry and hostnames](../explanation/geometry-and-hostnames.md).

## What the session is

A real PTY. Full-screen programs work: `vim`, `htop`, `watch`, `less`, anything that wants a
terminal rather than a pipe. Resizing the pane resizes the remote terminal, so a curses layout
redraws correctly instead of wrapping.

It is the app's connection, which has two consequences worth being deliberate about:

- **You are the app's SSH identity, not your own.** The session runs as the cluster's configured
  `ssh_user` with the app's key. Two operators with terminal access have the same access to the
  cluster, and the audit trail records which *Greenlight* account opened the session.
- **It spends a session slot.** The head node's `sshd MaxSessions` bounds terminals and fan-outs
  together. A handful of forgotten terminals on a 10-slot host is enough to make a fleet sweep
  start losing nodes silently.

## Reconnect without losing your shell

A session outlives its WebSocket by a 30-second grace period. Close the tab and come back, lose
your network for a moment, or detach the pane into its own window, and the browser **adopts the
running shell** rather than opening a second SSH hop to the same host.

So a long `tail -f` survives a flaky connection. What it does not survive is the grace period:
after 30 seconds with no WebSocket attached the PTY is reaped, and the next visit is a fresh
shell.

## Keep work running after you close the browser

For anything that must outlive that, take it out of the terminal's hands. Start it under `tmux`
on the target host and detach with `Ctrl-b d`:

```bash
tmux new -s burnin
```

Re-attach from any later session:

```bash
tmux attach -t burnin
```

Better still, submit it as a tracked job rather than a shell command. A run started that way
survives a container restart, keeps per-step output, and shows progress an operator can see —
which a detached `tmux` session does not.

## Turn it off

On a deployment where interactive shell access is not appropriate, set the switch and restart:

```toml
[general]
RACK_ENABLE_WEB_TERMINAL = false
```

The middleware is not installed at all when this is false, so the endpoint does not exist rather
than existing and refusing — and because it is read at startup, no request can flip it. Confirm
it is gone rather than merely hidden:

```bash
curl -ki https://localhost:5050/ws/terminal | head -1   # expect 404, not 401
```

## Why it is authenticated differently

`/ws/terminal` is handled by WSGI middleware wrapping the app, *below* Flask's router, because
the WebSocket handshake writes its HTTP 101 straight onto the socket and never calls
`start_response` — something a Flask view cannot do.

Sitting below the router means the usual request hooks never see it, so the middleware performs
its own checks and refuses before upgrading:

- **the session must be authenticated** — an unauthenticated upgrade is rejected, not downgraded;
- **the `Origin` must be allowed** — a WebSocket is not subject to the same-origin policy the way
  `fetch` is, so a page on another origin could otherwise open a shell using your cookies.

This is the one endpoint whose gate is written out by hand rather than inherited. When changing
anything on that path, those two checks are the code to preserve.

## If it does not open

| Symptom | Cause |
|---|---|
| The menu item is absent | `RACK_ENABLE_WEB_TERMINAL` is off, so the endpoint was never installed. |
| Connects, then closes immediately | The session was unauthenticated or the `Origin` was refused. Reload to renew the session first. |
| Opens but the shell is dead | The SSH connection itself is failing. Try any rack action — if that fails too, it is the connection rather than the terminal. |
| Slow and stale, and so is everything else | A stale SSH ControlMaster. It never self-heals: clear `<mux dir>/nvis-mux-*` or restart the container. |

## Related

- [How to configure a deployment](configure-a-deployment.md) — where the connection is defined.
- [Projects and clusters](../reference/catalog/projects-and-clusters.md) — endpoints and roles.
