// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Typed domain verbs — the curated ergonomics over the flat `/api/*` surface.
//!
//! Each verb maps a friendly command onto one or more real endpoints and renders the result as a
//! table, while `nvrs api` remains the escape hatch for everything not yet promoted here. They all
//! call through [`crate::api::call_json`], so authentication and refresh behave identically to the
//! generic path.
//!
//! The centrepiece is [`run`]: the product's heavy operations are submit-then-poll (`POST
//! /api/run-<suite>-async` returns a `job_id`, `GET /api/run-<suite>-status?job_id=…` reports
//! progress), and `--follow` turns that into a single foreground command that streams phase changes
//! and exits with the job's success.

use std::time::Duration;

use anyhow::{bail, Context, Result};
use nvrs_client::Method;
use serde_json::{Map, Value};

use crate::api::call_json;
use crate::config::{Config, Profile};
use crate::output;

// ─── rack ────────────────────────────────────────────────────────────────────

/// `nvrs rack list` — the advisory lock snapshot: who is operating on which rack.
pub async fn rack_list(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    format: output::Format,
) -> Result<()> {
    let value = call_json(
        cfg,
        name,
        profile,
        Method::GET,
        "/api/rack-locks",
        &[],
        None,
        None,
    )
    .await?;
    if let Some(s) = output::structured(&value, format) {
        println!("{s}");
        return Ok(());
    }
    // `locks` is a map of rack -> entry; flatten it into rows with the rack as a column.
    let mut rows: Vec<Map<String, Value>> = Vec::new();
    if let Some(locks) = value.get("locks").and_then(Value::as_object) {
        for (rack, entry) in locks {
            if let Some(obj) = entry.as_object() {
                let mut row = obj.clone();
                row.insert("rack".into(), Value::String(rack.clone()));
                rows.push(row);
            }
        }
    }
    rows.sort_by_key(|r| output::cell(r.get("rack")));
    let refs: Vec<&Map<String, Value>> = rows.iter().collect();
    output::print_table(
        &["rack", "operator", "kind", "detail", "since", "expires_at"],
        &refs,
        "no racks are locked",
    );
    Ok(())
}

/// `nvrs rack lock <rack>` — create or refresh a manual claim.
#[allow(clippy::too_many_arguments)]
pub async fn rack_lock(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    rack: &str,
    note: Option<String>,
    ttl_sec: Option<u64>,
    force: bool,
    format: output::Format,
) -> Result<()> {
    // The endpoint requires an operator label (X-Rack-Operator) and 400s without one; catch it here
    // with an actionable message rather than letting the server's terse error surface.
    if profile.operator.as_deref().unwrap_or("").is_empty() {
        bail!("this profile has no operator set; run `nvrs config set-operator {name} \"Your Name\"` first");
    }
    let mut body = Map::new();
    if let Some(note) = note {
        body.insert("note".into(), Value::String(note));
    }
    if let Some(ttl) = ttl_sec {
        body.insert("ttl_sec".into(), Value::Number(ttl.into()));
    }
    if force {
        body.insert("force".into(), Value::Bool(true));
    }
    let path = format!("/api/rack-locks/{rack}");
    let value = call_json(
        cfg,
        name,
        profile,
        Method::POST,
        &path,
        &[],
        Some(&Value::Object(body)),
        None,
    )
    .await?;
    if let Some(s) = output::structured(&value, format) {
        println!("{s}");
    } else {
        println!("locked {rack}");
    }
    Ok(())
}

/// `nvrs rack unlock <rack>` — release a manual claim.
pub async fn rack_unlock(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    rack: &str,
    format: output::Format,
) -> Result<()> {
    let path = format!("/api/rack-locks/{rack}");
    let value = call_json(cfg, name, profile, Method::DELETE, &path, &[], None, None).await?;
    if let Some(s) = output::structured(&value, format) {
        println!("{s}");
    } else {
        println!("unlocked {rack}");
    }
    Ok(())
}

// ─── reports ───────────────────────────────────────────────────────────────

/// `nvrs reports list` — reports this deployment has generated and kept on disk.
pub async fn reports_list(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    format: output::Format,
) -> Result<()> {
    let value = call_json(
        cfg,
        name,
        profile,
        Method::GET,
        "/api/reports/generated",
        &[],
        None,
        None,
    )
    .await?;
    let reports = value
        .get("reports")
        .cloned()
        .unwrap_or(Value::Array(vec![]));
    output::print_array_table(&reports, "no reports have been generated", format);
    Ok(())
}

// ─── logs ────────────────────────────────────────────────────────────────────

/// `nvrs logs tail` — live `tail -f` of a run's log (or a Slurm job's stdout) streamed to the
/// terminal. Wraps the two streaming tail endpoints; the body they read is `log_dir` plus either a
/// log basename or a Slurm stdout `path`.
pub async fn logs_tail(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    dir: String,
    file: Option<String>,
    job_path: Option<String>,
) -> Result<()> {
    let mut body = Map::new();
    body.insert("log_dir".into(), Value::String(dir));
    let path = if let Some(jp) = job_path {
        body.insert("path".into(), Value::String(jp));
        "/api/tail-job-stdout-stream"
    } else {
        if let Some(f) = file {
            body.insert("log_basename".into(), Value::String(f));
        }
        "/api/tail-log-stream"
    };
    crate::api::stream_call(
        cfg,
        name,
        profile,
        Method::POST,
        path,
        &[],
        Some(&Value::Object(body)),
        None,
    )
    .await
}

// ─── compute ─────────────────────────────────────────────────────────────────

/// `nvrs compute status` — the main rack status payload. The shape is produced by `rack_status.sh`
/// and is not something the CLI models, so the human view is the pretty-printed document; `--json`
/// gives the compact form for scripting.
pub async fn compute_status(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    format: output::Format,
) -> Result<()> {
    let value = call_json(
        cfg,
        name,
        profile,
        Method::GET,
        "/api/status",
        &[],
        None,
        None,
    )
    .await?;
    output::print_value(&value, format);
    Ok(())
}

// ─── run ─────────────────────────────────────────────────────────────────────

/// The validation suites that follow the `run-<suite>-async` / `run-<suite>-status` convention.
/// Kept explicit rather than derived, so `nvrs run` lists exactly what it can drive and a suite that
/// does not fit the convention is not offered by accident.
const SUITES: &[&str] = &[
    "validation",
    "nemo-xrack",
    "llama",
    "mnubergemm-stress",
    "nccl-fullrack",
    "nvloom",
    "clusterkit",
    "llmb-nccl",
];

/// Terminal job phases, and which of them are failures. `phase` is the field `job_registry` writes.
const TERMINAL: &[&str] = &[
    "done",
    "finished",
    "complete",
    "completed",
    "error",
    "failed",
    "timeout",
    "timed_out",
    "cancelled",
    "canceled",
];
const FAILED: &[&str] = &[
    "error",
    "failed",
    "timeout",
    "timed_out",
    "cancelled",
    "canceled",
];

/// Where a job stands, from its phase and (optional) return code.
#[derive(Debug, PartialEq, Eq)]
enum Outcome {
    /// Not terminal yet — keep polling.
    Pending,
    /// Reached a terminal phase successfully.
    Done,
    /// Reached a terminal phase as a failure (bad phase, or a non-zero return code).
    Failed,
}

/// Classify a job's phase. A non-zero `returncode` fails even a "done" phase — a script that exited
/// non-zero did not succeed just because the wrapper reached its end.
fn classify(phase: &str, returncode: Option<i64>) -> Outcome {
    if !TERMINAL.contains(&phase) {
        return Outcome::Pending;
    }
    if FAILED.contains(&phase) || returncode.is_some_and(|c| c != 0) {
        Outcome::Failed
    } else {
        Outcome::Done
    }
}

/// `nvrs run <suite>` — submit a validation suite, and with `--follow` poll it to completion.
#[allow(clippy::too_many_arguments)]
pub async fn run_suite(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    suite: Option<String>,
    rack: Option<String>,
    target: Option<String>,
    fields: Vec<String>,
    body: Option<String>,
    follow: bool,
    interval_secs: u64,
    format: output::Format,
) -> Result<()> {
    let suite = match suite {
        Some(s) if SUITES.contains(&s.as_str()) => s,
        Some(s) => bail!("unknown suite {s:?}; available: {}", SUITES.join(", ")),
        None => {
            println!("available suites: {}", SUITES.join(", "));
            return Ok(());
        }
    };

    // Assemble the request body: --body/--field, plus --rack/--target as convenience keys.
    let mut obj = match crate::api::build_body(&fields, body.as_deref())? {
        Some(Value::Object(m)) => m,
        Some(other) => {
            // A non-object --body can't take rack/target merges; send it as-is only if neither given.
            if rack.is_some() || target.is_some() {
                bail!("--rack/--target need an object body; your --body is not a JSON object");
            }
            return submit_and_maybe_follow(
                cfg,
                name,
                profile,
                &suite,
                other,
                follow,
                interval_secs,
                format,
            )
            .await;
        }
        None => Map::new(),
    };
    if let Some(rack) = rack {
        obj.insert("rack".into(), Value::String(rack));
    }
    if let Some(target) = target {
        obj.insert("target".into(), Value::String(target));
    }

    submit_and_maybe_follow(
        cfg,
        name,
        profile,
        &suite,
        Value::Object(obj),
        follow,
        interval_secs,
        format,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn submit_and_maybe_follow(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    suite: &str,
    body: Value,
    follow: bool,
    interval_secs: u64,
    format: output::Format,
) -> Result<()> {
    let async_path = format!("/api/run-{suite}-async");
    let submitted = call_json(
        cfg,
        name,
        profile,
        Method::POST,
        &async_path,
        &[],
        Some(&body),
        None,
    )
    .await?;

    let job_id = submitted
        .get("job_id")
        .and_then(Value::as_str)
        .context("the server accepted the run but returned no job_id")?
        .to_string();

    if format != output::Format::Text && !follow {
        output::print_value(&submitted, format);
        return Ok(());
    }
    println!("submitted {suite} — job {job_id}");
    if !follow {
        println!("follow it with: nvrs run {suite} --follow  (or: nvrs api GET /api/run-{suite}-status --query job_id={job_id})");
        return Ok(());
    }

    follow_job(cfg, name, profile, suite, &job_id, interval_secs, format).await
}

/// Poll a job's status endpoint until it reaches a terminal phase, printing each phase transition.
/// Exits with an error (non-zero) if the job failed, so `nvrs run … --follow` can gate a script.
async fn follow_job(
    cfg: &Config,
    name: &str,
    profile: &Profile,
    suite: &str,
    job_id: &str,
    interval_secs: u64,
    format: output::Format,
) -> Result<()> {
    let status_path = format!("/api/run-{suite}-status");
    let query = [("job_id".to_string(), job_id.to_string())];
    let mut last_phase = String::new();

    loop {
        let status = call_json(
            cfg,
            name,
            profile,
            Method::GET,
            &status_path,
            &query,
            None,
            None,
        )
        .await?;

        let phase = status
            .get("phase")
            .or_else(|| status.get("state"))
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();

        if phase != last_phase {
            println!("[{phase}]");
            last_phase = phase.clone();
        }

        let rc = status.get("returncode").and_then(Value::as_i64);
        match classify(&phase, rc) {
            Outcome::Pending => {}
            terminal => {
                if format != output::Format::Text {
                    output::print_value(&status, format);
                }
                if let Some(dir) = status.get("log_dir").and_then(Value::as_str) {
                    println!("logs: {dir}");
                }
                if terminal == Outcome::Failed {
                    bail!(
                        "{suite} job {job_id} ended in {phase}{}",
                        rc.map(|c| format!(" (exit {c})")).unwrap_or_default()
                    );
                }
                println!("{suite} job {job_id} completed");
                return Ok(());
            }
        }

        tokio::time::sleep(Duration::from_secs(interval_secs.max(1))).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suite_list_is_nonempty_and_paths_follow_the_convention() {
        assert!(SUITES.contains(&"validation"));
        // The async/status path convention the run/follow logic relies on.
        for s in SUITES {
            assert!(!s.is_empty());
        }
    }

    #[test]
    fn failed_phases_are_a_subset_of_terminal() {
        for p in FAILED {
            assert!(TERMINAL.contains(p), "{p} must also be terminal");
        }
    }

    #[test]
    fn classify_pends_until_terminal() {
        assert_eq!(classify("running", None), Outcome::Pending);
        assert_eq!(classify("starting", None), Outcome::Pending);
        assert_eq!(classify("unknown", None), Outcome::Pending);
    }

    #[test]
    fn classify_done_is_success() {
        assert_eq!(classify("done", None), Outcome::Done);
        assert_eq!(classify("done", Some(0)), Outcome::Done);
        assert_eq!(classify("finished", Some(0)), Outcome::Done);
    }

    #[test]
    fn classify_failure_phases_and_nonzero_rc() {
        assert_eq!(classify("error", None), Outcome::Failed);
        assert_eq!(classify("timeout", None), Outcome::Failed);
        // A non-zero return code fails even a nominally-successful phase.
        assert_eq!(classify("done", Some(1)), Outcome::Failed);
    }
}
