# NC ClusterKit Live API Validation

The AppServer handoff `NC-ClusterKit-API-live-20261022-handoff.tar.gz` reports
successful one-, two-, four- and eight-node workflows through the complete Flask
application and real NC Slurm. Requests used Flask's in-process client in an
isolated environment; the running Rack Status deployment was unchanged.

| Scope | Slurm job | Expected pairs per metric and mode | Functional | Performance |
| --- | --- | --- | --- | --- |
| One-node preflight | 219620 | 0 | PASS | UNVERIFIED |
| Two-node explicit | 219621 | 1 | PASS | UNVERIFIED |
| Four-node configured group | 219622 | 6 | PASS | UNVERIFIED |
| Eight-node explicit | 219623 | 28 | PASS | UNVERIFIED |

Baseline and single-path runs had complete bandwidth and latency pair coverage.
Duplicate confirmations, process restarts, initially empty registry recovery,
result/artifact retrieval, authentication and exact-directory history queries
were verified. The AppServer report records 426 software tests, 208 subtests and
130 standalone tests passing.

## Imported Fix

The initial four-node attempt, job 219617, failed when healthy UCX device
enumeration exceeded its five-second timeout. A separate diagnostic completed
the same query in 6.916 seconds. The imported three-file patch raises only the
device-inventory query timeout to 15 seconds, records elapsed time on success
and failure, and adds two simulated regression tests. Overall preflight/job
limits and static execution scripts remain unchanged. The original failed
attempt remains failed and retains its evidence.

The patch was checked against local commit `67f54c1`, and all three resulting
source hashes match the handoff's `source-verification.json`. Mac verification
after applying it: 207 targeted tests and 208 subtests passed; scoped Ruff and
`git diff --check` passed. No Mac-side live job or deployment was performed.

## Frontend Contract

- Keep the application activity UUID distinct from the numeric Slurm job ID.
- Use reconciled API scheduler status; immutable compute-side summaries can
  legitimately retain an earlier `UNKNOWN` scheduler state.
- Show functional PASS separately from performance UNVERIFIED. Complete pair
  coverage is not fabric performance acceptance.
- Shared activity `log_dir` filters match an exact attempt directory. A parent
  results-root query is not recursive and will hide these runs.
- Retain failed attempts alongside subsequent successful attempts.
- A gpu55 `duplicate_request` counter increased by 120 in one two-node run.
  Keep this observation available for fabric-team interpretation without
  assigning a cause or labelling performance accepted.

The original archive in Downloads retains the complete report, raw attempts,
registries, diagnostics and test logs. These are not copied into the source tree.
