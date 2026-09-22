---
diataxis: explanation
description: Why Greenlight polls PythonCM from durable task files instead of running a broker daemon on the head node, what the cheap path cannot see, and what would reverse the decision.
---

# Why there is no PythonCM broker

The implementation this scheduler was reconstructed from runs a **broker**: a long-lived
process on the BCM head that owns one connected `pythoncm` client and answers requests over a
Unix socket. We did not take it. This records why, and — more usefully — what would make that
the wrong answer.

## The problem it solves is real

Connecting a `pythoncm` client is expensive. The broker's own docstring puts it at
**roughly ten CPU-seconds on the Nscale 16K controller**, and nothing we have contradicts
that. The cost is per *connection*, not per call: once connected, queries are fast.

That number only hurts where connections are frequent, and there is exactly one such place.
A submit pays it once, which is nothing next to the job. A cancel pays it once. But **status
polling** pays it on every tick, and polling is unbounded: an hour-long job polled every five
seconds is 720 connections. At ten CPU-seconds each that is two CPU-hours of controller time
spent *watching* a job — comfortably more than most jobs cost to run, on the one machine in
the cluster that must stay responsive.

So the broker is not micro-optimisation. Left unaddressed, this makes polling unusable.

## Why we solved it differently

A daemon is a heavy answer to "connecting is slow", and it brings four costs that are easy
to overlook while looking at the latency number:

**It is a second thing to install and keep alive.** Two systemd units, a runtime directory, a
socket group. `storage-engines.mdc` refuses a new storage engine on exactly this reasoning —
"a service to install is a service the operator has to keep alive" — and the argument does not
change because the service speaks a scheduler protocol instead of a query language. Several of
our deployments are air-gapped appliances.

**It is a second transport.** Every other command in this product crosses to a host through
`Connection` (`core-designs.mdc` §1). A Unix socket beside it is a path that knows nothing
about the multi-hop chain, the env forwarding or the profile split, and the *reason* it can
get away with that is that it only ever runs head-local — which means the app must already
have a way to reach the head, and now has two.

**It is unauthenticated.** Access control is the socket mode and a Unix group. That is
defensible for a host-local socket, but it is a different security model from everything
else we run, on a machine where `cmods` is a shared account.

**It needed a watchdog.** The strongest signal is in the source itself: the fork ships a
systemd timer that probes the broker every minute and restarts it when it stops answering,
plus an hourly audit counting `broker_disconnect_timeout` in the logs. A component that needs
a babysitter to stay up is a component whose failure modes you inherit.

Against that, the saving is available another way, because **the expensive question is one we
mostly do not need to ask**. The helper already writes durable per-task state: a manifest, and
one `stdout`/`stderr`/`status` triple per node. The status file is written last and by rename,
so *its existence means that node finished*. A job whose nodes have all written one is
finished, and that is a fact on disk — no client, no daemon, no socket.

So `nvis_pythoncm_exec.py` grew `--offline`, and the provider polls with it. A poll costs one
SSH round trip. CMDaemon is consulted only when the files cannot answer.

## What the cheap path cannot see, and what we do about it

Offline, **a job still running and a job whose ranks died look identical**: neither has written
all its status files. Disk can prove completion; it cannot prove liveness.

The provider therefore runs one authoritative check every `_VERIFY_EVERY` polls — twelve, so
about once a minute at the default interval. A dead job is noticed within a minute rather than
instantly, and the controller pays one connection per minute rather than twelve. The offline
reading carries `authoritative: false` so nothing downstream mistakes the guess for a finding.

Two smaller decisions follow from the same reasoning. A **cancel is never offline**: a cancel
that did not reach CMDaemon has not cancelled anything, and the leftover ranks are what drain
the next job's rack (`bcm-prolog-rogueprocess.mdc`). And `status()` — the public one-shot
question, asked by callers about to act — is always authoritative; only the internal `wait`
loop uses the cheap path.

## What would change this answer

This is a judgement on present evidence, not a principle. Reopen it if:

- **Inventory polling moves to PythonCM.** The saving above is specific to *job* polling, where
  durable files exist. `bcm_query` has no equivalent — an inventory refresh genuinely needs a
  live client, and if the dashboard's node inventory starts coming from `pythoncm` rather than
  `cmsh`, the ten seconds lands on a path with no cheap alternative. This is the most likely
  trigger.
- **We measure the cost ourselves and it is worse than ten seconds**, or it applies to the
  helper's startup rather than the client's construction. The figure is one line in one
  docstring about one controller; we have not reproduced it.
- **A site needs sub-second job state.** The verification cadence bounds how stale a liveness
  answer can be. A minute is fine for a validation run and wrong for an interactive scheduler.

If it is reopened, the thing to preserve is the boundary rather than the daemon: keep the
broker head-local and keep `Connection` as the way the app reaches the head, so a broker is an
optimisation behind the existing transport rather than a second route to the cluster.

## The watchdog we did keep, and the one we did not

The fork's watchdog exists **only** to restart the broker; that is its entire body. Without a
broker there is nothing for it to restart, and porting it would have meant porting the remedy
for a problem we chose not to create.

What survives is the *observation* half, as `backend/scheduler_watch.py`: a periodic sample of
whether the active cluster's scheduler can accept work, so an operator can ask **when** it
stopped answering rather than only whether it is answering now. It does not restart anything,
and it is not called a watchdog, because a watchdog that cannot act is a monitor.

It is also deliberately not PythonCM-shaped. It asks whichever provider the active cluster
resolves to, and a provider with no cheap readiness check reports `ready: null` rather than
being skipped — so a Slurm site gets an honest "not applicable" instead of a check that
silently does nothing, and switching clusters switches what is watched.
