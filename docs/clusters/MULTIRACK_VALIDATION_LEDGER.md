# Multi-Rack Validation Run Ledger

Every validation activity whose `scope` is `multirack` is copied into an append-preserving
ledger. This is separate from the live activity table: deleting a UI row or reaching the
activity table's retention limit does not delete the audit record.

## Outputs

- Canonical archive: `var/reports/multirack_validation_runs.json`
- Human-readable document: `var/reports/MULTIRACK_VALIDATION_RUNS.md`
- Parquet delivery status/outbox: `var/cache/multirack_ledger_parquet_status.json`
- Parquet data: the existing `RACK_LAKE_URL` lakewriter receives `multirack-ledger` snapshots
  and writes them to the `run_artifacts` Parquet table.

The three local paths can be overridden with `RACK_MULTIRACK_LEDGER_FILE`,
`RACK_MULTIRACK_LEDGER_MARKDOWN_FILE`, and
`RACK_MULTIRACK_LEDGER_EXPORT_STATUS_FILE`. Set
`RACK_MULTIRACK_LEDGER_LAKE_DISABLED=1` only when Parquet delivery must be disabled.

The app does not need `pyarrow`: Parquet encoding belongs to lakewriter. Delivery runs on one
daemon worker with semantic deduplication, so a launch never waits for lake I/O. A pending
snapshot left by a process restart is automatically re-queued by the new process.

## HTTP

Read or download the generated document:

```text
GET /api/validation-activities/ledger
```

Record a root cause, outcome note, or corrective action without replacing earlier actions:

```http
POST /api/validation-activities/<activity-id>/corrective-actions
Content-Type: application/json

{
  "issue_summary": "Why the run failed",
  "outcome_summary": "What the run did or did not complete",
  "action": "Corrective action taken",
  "status": "complete",
  "result": "Verification performed"
}
```

At least one of `issue_summary`, `outcome_summary`, or `action` is required. Repeated calls
append actions; they do not overwrite prior entries.

## Outcome semantics

The wrapper returning zero means only that Slurm accepted the submission. When a scheduler task
exists, the ledger reports its `work_phase`/Slurm state. A wrapper marked `done` with a failed
task is therefore recorded as `FAILED`, not as a successful run.

## Deployment and verification

Run `bin/prod-drift.sh` before deployment. On Nscale TX, `backend/app.py` and
`backend/validation_activities.py` are bind-mounted into the rack-status container, so the
container must be restarted to load the new Python code. No package, image rebuild, script sync,
or new bind mount is required.

After restart, the first validation activity change or ledger GET imports existing multi-rack
activities into the archive. Verify:

```bash
curl -fsS http://localhost:5056/api/validation-activities/ledger
docker exec nvis-rack-status-v3 sh -lc \
  'ls -l /app/var/reports/MULTIRACK_VALIDATION_RUNS.md /app/var/cache/multirack_ledger_parquet_status.json'
```

For each exported entry, `last_result.ok` must be `true`. Lakewriter stores matching records with
workload `multirack-ledger`, the stable activity UUID in the run ID, and the Slurm reference in
the `job_id` and parsed `scheduler_job_ids` columns.
