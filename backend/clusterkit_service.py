# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Confirmed ClusterKit lifecycle with durable evidence and no automatic cancellation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import clusterkit_artifacts as safe_artifacts
import clusterkit_context as execution_context
from clusterkit_attempts import (
    load_intent,
    receipt,
    reject_unresolved_overlap,
    submission_lock,
    write_record,
)
from clusterkit_execution import SOURCE_FILES, digest, prepare_attempt, source_path
from clusterkit_job import job_spec
from clusterkit_planner import (
    SlurmInventory,
    build_plan,
    candidates_for,
    parse_request,
    plan_request,
)
from clusterkit_profile import ProfileResolution
from clusterkit_summary import validate_summary
from schedulers import JobHandle
from schedulers.base import TERMINAL_STATES

TOKEN_VERSION = 2
DEFAULT_PREVIEW_TTL_SECONDS = 300
MAX_PREVIEW_TTL_SECONDS = 900
MAX_ARTIFACT_BYTES = safe_artifacts.MAX_ARTIFACT_BYTES
_logger = logging.getLogger(__name__)


class ConfirmationError(ValueError):
    """Preview confirmation is invalid, expired or bound to different execution."""


class DuplicateSubmissionError(RuntimeError):
    """An existing attempt is never submitted again."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _collect_grace_seconds():
    from config_env import read_only_values
    try:
        value = read_only_values(('RACK_CLUSTERKIT_COLLECT_GRACE_SEC',))['RACK_CLUSTERKIT_COLLECT_GRACE_SEC']
        return max(30, min(1800, int(value or '300')))
    except (TypeError, ValueError):
        return 300


def _age_seconds(value):
    try:
        started = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _secret_bytes(secret):
    if isinstance(secret, str):
        secret = secret.encode()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise RuntimeError('A stable application secret of at least 32 bytes is required')
    return secret


def profile_digest(resolution):
    if not resolution.ready or resolution.profile is None:
        raise ConfirmationError('ClusterKit profile is disabled or invalid')
    return _hash(resolution.profile.as_dict())


def execution_digest(plan):
    # Discovery timestamps, availability and warnings are intentionally revalidated,
    # not frozen. Every execution-affecting product/resource/argv field is bound.
    return _hash({key: plan[key] for key in ('schema_version', 'attempt_id', 'scope',
        'node_count', 'nodes', 'request', 'effective_profile', 'scripts_directory',
        'resources', 'results_directory', 'entrypoint', 'entrypoint_supported',
        'sbatch_argv', 'expected_pairs', 'expected_pair_count', 'coverage_mode')})


def recipe_digest(plan):
    files = set(SOURCE_FILES) | {'backend/clusterkit_service.py', 'backend/clusterkit_context.py',
        'backend/clusterkit_artifacts.py', 'backend/clusterkit_attempts.py',
        'backend/clusterkit_summary.py', 'backend/schedulers/slurm.py',
        'backend/schedulers/base.py', 'backend/platforms/base.py',
        'backend/clusterkit_inventory.py', 'backend/cluster_snapshot.py'}
    return _hash({'sources': {name: digest(source_path(name, plan['scripts_directory'])) for name in sorted(files)},
                  'hpcx_init': digest(plan['effective_profile']['hpcx_init'])})


def sign_preview(plan, secret, *, context=None, now=None, ttl_seconds=DEFAULT_PREVIEW_TTL_SECONDS):
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_PREVIEW_TTL_SECONDS:
        raise ValueError('Invalid preview lifetime')
    context = context or execution_context.capture(plan['scripts_directory'])
    payload = {'version': TOKEN_VERSION, 'expires_at': int(time.time() if now is None else now) + ttl_seconds,
        'attempt_id': plan['attempt_id'], 'request': plan['request'], 'nodes': plan['nodes'],
        'execution_digest': execution_digest(plan), 'recipe_digest': recipe_digest(plan),
        'profile_hash': _hash(plan['effective_profile']), 'context': context}
    encoded = _b64(_json(payload))
    return encoded + '.' + _b64(hmac.new(_secret_bytes(secret), encoded.encode(), hashlib.sha256).digest())


def verify_preview(token, secret, *, now=None):
    if (not isinstance(token, str) or not token.isascii()
            or len(token) > 32768 or token.count('.') != 1):
        raise ConfirmationError('Invalid preview confirmation')
    encoded, supplied = token.split('.', 1)
    expected = _b64(hmac.new(_secret_bytes(secret), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        raise ConfirmationError('Invalid preview confirmation signature')
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    except (ValueError, TypeError) as exc:
        raise ConfirmationError('Invalid preview confirmation payload') from exc
    if (not isinstance(payload, dict) or payload.get('version') != TOKEN_VERSION
            or type(payload.get('expires_at')) is not int
            or payload['expires_at'] <= int(time.time() if now is None else now)):
        raise ConfirmationError('Preview confirmation expired or has an unsupported version')
    return payload


def readiness(resolution: ProfileResolution) -> dict[str, Any]:
    response = resolution.public()
    if resolution.profile is None:
        return response
    profile = resolution.profile
    if profile.rack_inventory:
        snapshot = (resolution.inventory or {}).get("snapshot", {})
        response.update(rack_inventory_enabled=True, rack_inventory_ready=snapshot.get("state") == "fresh",
                        rack_inventory_snapshot=snapshot,
                        rack_inventory_error=(resolution.inventory or {}).get("error"))
    response.update({
        "whole_rack_enabled": profile.whole_rack_enabled,
        "partition": profile.partition,
        "path_comparison": profile.path_comparison,
        "explicit_nodes": list(profile.node_allowlist),
        "regex_selection_enabled": bool(profile.node_regex),
        "groups": [
            {
                "name": group.name,
                "kind": group.kind,
                "authoritative": group.authoritative,
                "source": group.source,
                "nodes": list(group.nodes),
                "node_count": len(group.nodes),
            }
            for group in profile.topology_groups
        ],
    })
    return response


def inventory(resolution: ProfileResolution, reader: SlurmInventory, *, group: str = "") -> dict[str, Any]:
    if not resolution.ready or resolution.profile is None:
        raise ValueError("ClusterKit profile is disabled or invalid")
    profile = resolution.profile
    if group:
        selected = next((entry for entry in profile.topology_groups if entry.name == group), None)
        if selected is None:
            raise ValueError("Unknown rack or configured group")
        snapshot = reader.query(selected.nodes)
        snapshot["group"] = group
        for row in snapshot["nodes"]:
            row["allowed"] = profile.allows(row["node"])
        return snapshot
    candidates = list(profile.node_allowlist)
    if not candidates:
        candidates = sorted({node for group in profile.topology_groups for node in group.nodes})
    if not candidates:
        return {"source": "slurm", "captured_at": None, "nodes": [],
                "note": "No finite configured node inventory; preview explicit nodes to query them."}
    candidates = [node for node in candidates if profile.allows(node)]
    if len(candidates) > 512:
        return {"source": "slurm", "captured_at": None, "nodes": [],
                "note": "Select a rack to refresh its bounded Slurm inventory."}
    return reader.query(tuple(candidates)) if candidates else {"source": "slurm", "captured_at": None, "nodes": []}


def preview(raw_request, resolution, reader, *, scripts_dir, secret, context=None,
            now=None, ttl_seconds=DEFAULT_PREVIEW_TTL_SECONDS):
    context = context or execution_context.capture(scripts_dir, getattr(reader, 'connection', None))
    plan = plan_request(raw_request, resolution, reader, scripts_dir=scripts_dir)
    if context['scripts_directory'] != scripts_dir:
        raise ValueError('Execution scripts must use their canonical local directory')
    command = execution_context.PinnedPlatform(context).sbatch_cmd(shlex.join(plan['sbatch_argv'][1:])) if plan['sbatch_argv'] else None
    activity_id = str(uuid.UUID(plan['attempt_id']))
    return {'plan': plan, 'execution_context': context, 'submission_command_display': command,
            'activity_id': activity_id, 'status_url': f'/api/clusterkit/runs/{activity_id}',
            'result_url': f'/api/clusterkit/runs/{activity_id}',
            'confirmation_token': sign_preview(plan, secret, context=context, now=now, ttl_seconds=ttl_seconds),
            'confirmation_expires_in_seconds': ttl_seconds, 'confirmation_required': True}


def confirm_plan(token, resolution, reader, *, scripts_dir, secret, context=None, now=None):
    payload = verify_preview(token, secret, now=now)
    if resolution.profile and resolution.profile.rack_inventory:
        from clusterkit_inventory import require_current_inventory
        try:
            require_current_inventory(resolution)
        except ValueError as exc:
            raise ConfirmationError(str(exc)) from exc
    if profile_digest(resolution) != payload.get('profile_hash'):
        raise ConfirmationError('ClusterKit site policy changed after preview')
    try:
        context = context or execution_context.capture(scripts_dir, getattr(reader, 'connection', None))
        execution_context.require_same(payload.get('context'), context)
        request = parse_request(payload['request'])
        plan = build_plan(payload['request'], resolution, reader.query(candidates_for(resolution, request)),
                          scripts_dir=scripts_dir, attempt_id=payload['attempt_id'])
        if plan['nodes'] != payload.get('nodes'):
            raise ValueError('Selected nodes changed after preview')
        if execution_digest(plan) != payload.get('execution_digest'):
            raise ValueError('Execution plan changed after preview')
        if recipe_digest(plan) != payload.get('recipe_digest'):
            raise ValueError('Reviewed recipe or HPC-X init changed after preview')
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ConfirmationError(f'Preview can no longer be confirmed: {exc}') from exc
    return plan


def _register(intent, plan, registry):
    spec = job_spec(plan)
    registry.record_activity('clusterkit', stage='preflight' if plan['scope'] == 'preflight' else 'pairwise',
        scope=plan['scope'], log_dir=plan['results_directory'], operator=intent['operator'],
        command_display=intent['submission_command_display'], scheduler='slurm', execution_provider='slurm',
        source='clusterkit-api', activity_id=intent['activity_id'], nodes=plan['nodes'],
        tasks=[{'task_id': intent['task_id'], 'ordinal': 1, 'label': spec.name, 'phase': 'starting',
                'work_phase': 'pending', 'scheduler_ref': None, 'scheduler_state': None,
                'evidence': 'clusterkit-normalized-summary', 'stdout_path': spec.output,
                'results_log_path': str(Path(plan['results_directory']) / 'summary.json'),
                'attempt_id': plan['attempt_id'], 'plan_hash': plan['plan_hash']}])
    recorded = registry.get_activity(intent['activity_id'])
    if not isinstance(recorded, dict) or _task(recorded).get('attempt_id') != plan['attempt_id']:
        raise RuntimeError('Durable activity write could not be verified; no job was submitted')


def _track(intent, plan, registry, reference, state='UNKNOWN'):
    registry.add_or_update_task(intent['activity_id'], ordinal=1, label=job_spec(plan).name,
        task_id=intent['task_id'], scheduler_ref=reference, scheduler_state=state, phase='running',
        work_phase='submitted' if reference else 'submission_unknown',
        stdout_path=str(Path(plan['results_directory']) / f'slurm-{reference}.out') if reference else '',
        evidence='clusterkit-normalized-summary',
        results_log_path=str(Path(plan['results_directory']) / 'summary.json'))
    registry.update_activity(intent['activity_id'], phase='running',
        work_phase='submitted' if reference else 'submission_unknown')
    recorded = registry.get_activity(intent['activity_id'])
    if not isinstance(recorded, dict) or _task(recorded).get('scheduler_ref') != reference:
        raise RuntimeError('Durable scheduler tracking write could not be verified')


def _receipt_value(intent, reference, recovered=False):
    return {'schema_version': 1, 'attempt_id': intent['attempt_id'], 'job_id': reference,
            'context': intent['context'], 'recorded_at': _now_iso(), 'recovered': recovered}


def submit_confirmed(token, resolution, reader, scheduler, registry, *, scripts_dir,
                     secret, operator='', context=None, now=None):
    context = context or execution_context.capture(scripts_dir, getattr(reader, 'connection', None))
    plan = confirm_plan(token, resolution, reader, scripts_dir=scripts_dir, secret=secret, context=context, now=now)
    with submission_lock(plan['effective_profile']['results_root']):
        # The same attempt remains a duplicate even when its submission was uncertain.
        if Path(plan['results_directory']).exists():
            raise DuplicateSubmissionError('This attempt already exists; recover it instead of resubmitting')
        reject_unresolved_overlap(plan, context)
        return _submit_claimed(plan, context, scheduler, registry, scripts_dir, operator)


def _submit_claimed(plan, context, scheduler, registry, scripts_dir, operator):
    # Reject symlink ancestors before the existing exclusive preparation operation.
    with safe_artifacts.directory(plan['effective_profile']['results_root']):
        pass
    try:
        prepared = prepare_attempt(plan, scripts_dir)
    except FileExistsError as exc:
        raise DuplicateSubmissionError('This attempt already exists; recover it instead of resubmitting') from exc
    intent = {'schema_version': 1, 'attempt_id': plan['attempt_id'],
        'activity_id': str(uuid.UUID(plan['attempt_id'])), 'task_id': str(uuid.uuid4()),
        'operator': operator, 'created_at': _now_iso(), 'context': context,
        'submission_command_display': execution_context.PinnedPlatform(context).sbatch_cmd(shlex.join(plan['sbatch_argv'][1:])),
        'plan_hash': plan['plan_hash'], 'execution_digest': execution_digest(plan),
        'recipe_digest': recipe_digest(plan), 'job_name': job_spec(plan).name,
        'comment': 'rack-clusterkit:' + plan['attempt_id']}
    root = plan['results_directory']
    write_record(root, 'submission-intent.json', intent)
    _register(intent, plan, registry)  # Failure here has not contacted the scheduler.
    write_record(root, 'submission-started.json', {'started_at': _now_iso()})
    reference, warnings = None, []
    try:
        handle = scheduler.submit(job_spec(plan))
        reference = handle.job_id
        if not isinstance(reference, str) or not reference.isdigit() or int(reference) <= 0:
            raise RuntimeError('Submission returned no valid job reference; acceptance is unknown')
    except Exception as exc:  # noqa: BLE001 - Acceptance may precede any lost/error reply.
        reference = None
        warnings.append('Submission acceptance is unknown; no retry or automatic cancellation was performed')
        try:
            write_record(root, 'submission-error.json', {'error': str(exc), 'at': _now_iso(), 'state': 'SUBMISSION_UNKNOWN'})
        except OSError:
            _logger.exception('Could not persist ambiguous submission error')
    if reference:
        try:
            write_record(root, 'submission-receipt.json', _receipt_value(intent, reference))
        except OSError:
            warnings.append('Job accepted but durable receipt write failed; recover by the unique attempt tag')
    try:
        _track(intent, plan, registry, reference)
    except Exception:
        warnings.append('Activity tracking degraded; retain this activity/attempt ID and reconcile its durable submission evidence')
        _logger.exception('ClusterKit activity tracking failed; accepted work is never cancelled')
    return {'ok': True, 'activity_id': intent['activity_id'], 'job_id': intent['activity_id'],
            'phase': 'running', 'work_phase': 'submitted' if reference else 'submission_unknown',
            'attempt_id': plan['attempt_id'],
            'scheduler_job_id': reference, 'submission_state': 'SUBMITTED' if reference else 'SUBMISSION_UNKNOWN',
            'results_directory': root, 'nodes': plan['nodes'], 'scope': plan['scope'],
            'prepared': prepared['prepared'], 'warnings': warnings,
            'status_url': f"/api/clusterkit/runs/{intent['activity_id']}",
            'result_url': f"/api/clusterkit/runs/{intent['activity_id']}"}


def _task(activity):
    tasks = [t for t in activity.get('tasks', []) if isinstance(t, dict)]
    if len(tasks) != 1:
        raise ValueError('ClusterKit activity must contain exactly one scheduler task')
    return tasks[0]


def _job_status_response(activity, scheduler_state, reference=None, **details):
    """Adapt the durable activity to Rack Status's existing job-polling envelope.

    job_id is the application activity UUID. scheduler_job_id is the separate
    numeric Slurm reference. Phase comes from evidence-backed reconciliation,
    never directly from a COMPLETED scheduler state.
    """
    uncertain = scheduler_state == 'SUBMISSION_UNKNOWN'
    return {'activity': activity, 'activity_id': activity['activity_id'],
            'job_id': activity['activity_id'], 'kind': 'clusterkit',
            'phase': 'running' if uncertain else activity.get('phase', 'starting'),
            'work_phase': 'submission_unknown' if uncertain else activity.get('work_phase', 'pending'),
            'returncode': None if uncertain else activity.get('returncode'),
            'error': activity.get('error'), 'tasks': activity.get('tasks', []),
            'target': activity.get('target') or ','.join(activity.get('nodes', [])),
            'command_display': activity.get('command_display', ''),
            'stdout_tail': activity.get('stdout_tail', ''), 'stderr_tail': activity.get('stderr_tail', ''),
            'scheduler_state': scheduler_state, 'scheduler_job_id': reference, **details}


def require_activity_context(activity, current=None):
    intent, plan = load_intent(activity['log_dir'], activity['activity_id'])
    execution_context.require_same(intent['context'], current or execution_context.capture(plan['scripts_directory']))
    return intent, plan


def recover_activity(activity_id, resolution, registry, *, context):
    """Rebuild missing registry state from a bounded search of existing attempt intents."""
    identifier = uuid.UUID(activity_id)
    if str(identifier) != activity_id or not resolution.ready:
        raise ValueError('Invalid recovery activity/profile')
    root = Path(resolution.profile.results_root)
    found = []
    with safe_artifacts.directory(root) as fd, os.scandir(fd) as entries:
        for index, entry in enumerate(entries):
            if index >= 512:
                raise ValueError('Recovery search exceeds 512 entries; use operator-assisted recovery')
            if entry.name.endswith('-' + identifier.hex) and entry.is_dir(follow_symlinks=False):
                found.append(root / entry.name)
    if len(found) != 1:
        raise KeyError('No unique durable attempt found')
    intent, plan = load_intent(found[0], activity_id)
    execution_context.require_same(intent['context'], context)
    if registry.get_activity(activity_id) is None:
        _register(intent, plan, registry)
    return registry.get_activity(activity_id)


def reconcile(activity_id, registry, scheduler, *, context=None):
    activity = registry.get_activity(activity_id)
    if not isinstance(activity, dict) or activity.get('tool') != 'clusterkit' or activity.get('source') != 'clusterkit-api':
        raise KeyError('Unknown modular ClusterKit activity')
    intent, plan = require_activity_context(activity, context)
    root = plan['results_directory']
    task = _task(activity)
    saved_receipt = receipt(root, intent)
    reference = saved_receipt['job_id'] if saved_receipt else None
    scheduler_error = None
    if reference and task.get('scheduler_ref') not in (None, '', reference):
        raise ValueError('Registry reference differs from durable receipt')
    if not reference:
        try:
            safe_artifacts.read_json(root, 'submission-started.json')
            handle = scheduler.find_by_tag(intent['job_name'], intent['comment'], intent['created_at'], intent['context']['user'])
            if handle:
                reference = handle.job_id
                try:
                    write_record(root, 'submission-receipt.json', _receipt_value(intent, reference, True))
                except FileExistsError:
                    saved_receipt = receipt(root, intent)
                    if saved_receipt['job_id'] != reference:
                        raise ValueError('Conflicting recovered submission receipts')
        except FileNotFoundError:
            scheduler_error = 'Attempt was prepared but submission was not started; no retry was performed'
        except Exception as exc:  # noqa: BLE001 - Preserve uncertain scheduler acceptance.
            scheduler_error = str(exc)
        if not reference:
            if not scheduler_error:
                scheduler_error = 'Submission acceptance remains unknown; no automatic retry'
            return _job_status_response(activity, 'SUBMISSION_UNKNOWN',
                    scheduler_error=scheduler_error, summary=None, submission_state='SUBMISSION_UNKNOWN')
    if task.get('scheduler_ref') != reference:
        _track(intent, plan, registry, reference)
        activity = registry.get_activity(activity_id) or activity
        task = _task(activity)
    state = str(task.get('scheduler_state') or 'UNKNOWN').upper()
    try:
        read_status = getattr(scheduler, 'observed_status', scheduler.status)
        observed = str(read_status(JobHandle(reference, 'slurm'))).upper().split()[0].rstrip('+')
        if observed == 'UNKNOWN':
            scheduler_error = 'Job absent from current accounting and queue snapshot'
        elif state not in TERMINAL_STATES:
            state = observed
    except Exception as exc:  # noqa: BLE001 - A provider error must preserve the last known state.
        scheduler_error = str(exc)
    summary, summary_error = None, None
    try:
        summary = safe_artifacts.read_json(root, 'summary.json')
        validate_summary(summary, plan, reference)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError) as exc:
        summary_error = str(exc)
    if state in TERMINAL_STATES or not scheduler_error:
        terminal = state in TERMINAL_STATES
        success = state == 'COMPLETED' and not summary_error and isinstance(summary, dict) and summary.get('functional_status') in {'PASS', 'WARN'}
        collecting_since = task.get('collecting_since')
        collecting = state == 'COMPLETED' and summary is None and not summary_error and (
            not collecting_since or _age_seconds(collecting_since) < _collect_grace_seconds())
        if collecting and not collecting_since:
            collecting_since = _now_iso()
        work = 'done' if success else 'collecting' if collecting else 'failed' if terminal else 'running'
        phase = 'done' if success else 'error' if terminal and not collecting else 'running'
        registry.add_or_update_task(activity_id, ordinal=1, label=intent['job_name'], task_id=intent['task_id'],
            scheduler_ref=reference, scheduler_state=state, phase=phase, work_phase=work,
            collecting_since=collecting_since if collecting else '',
            returncode=0 if success else 1 if terminal and not collecting else None,
            finished_at=_now_iso() if terminal and not collecting else None)
        registry.update_activity(activity_id, phase=phase, work_phase=work,
            returncode=0 if success else 1 if terminal and not collecting else None,
            error='' if success or collecting or not terminal else summary_error or (
                'Scheduler completed without a passing normalized ClusterKit summary' if state == 'COMPLETED' else f'Slurm job ended in {state}'),
            finished_at=_now_iso() if terminal and not collecting else None)
        activity = registry.get_activity(activity_id) or activity
    return _job_status_response(activity, state, reference, scheduler_error=scheduler_error,
            summary=summary, summary_error=summary_error)


def artifacts(activity):
    return safe_artifacts.artifacts(str(activity.get('log_dir') or ''))


def history(resolution, registry, *, context, limit=25, before=None):
    """History is scoped by pinned execution identity and exact attempt directories.

    Do not pass a parent results root to the shared registry's exact log_dir filter.
    Cursor pagination uses immutable creation time/id, so polling cannot reorder pages.
    """
    if not resolution.ready or resolution.profile is None:
        return {'runs': [], 'next_cursor': None}
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('History limit must be between 1 and 100')
    cursor = None
    if before:
        if len(before) > 1024:
            raise ValueError('Invalid history cursor')
        try:
            cursor = json.loads(base64.urlsafe_b64decode(before + '=' * (-len(before) % 4)))
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid history cursor') from exc
        if not isinstance(cursor, list) or len(cursor) != 2 or not all(isinstance(x, str) for x in cursor):
            raise ValueError('Invalid history cursor')
    root = Path(resolution.profile.results_root)
    rows = []
    for row in registry.activity_history():
        if row.get('source') != 'clusterkit-api' or row.get('tool') != 'clusterkit':
            continue
        attempt = Path(row.get('log_dir') or '')
        if attempt.parent != root:
            continue
        key = [str(row.get('created_at') or ''), str(row['activity_id'])]
        if cursor is not None and key >= cursor:
            continue
        try:
            intent, plan = load_intent(str(attempt), row['activity_id'])
        except (OSError, ValueError, RuntimeError):
            continue
        if not execution_context.same_context(intent.get('context'), context):
            continue
        rows.append({**row, 'node_count': len(plan['nodes']), 'expected_pair_count': plan['expected_pair_count'],
                     'attempt_id': plan['attempt_id'], 'path_comparison': plan['request']['path_comparison'],
                     'performance_status': 'UNVERIFIED'})
    rows.sort(key=lambda row: (row.get('created_at') or '', row['activity_id']), reverse=True)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last.get('created_at') or '', last['activity_id']]).encode()).decode().rstrip('=')
    return {'runs': page, 'next_cursor': next_cursor}


def artifact(activity, name):
    data = safe_artifacts.read_bytes(str(activity.get('log_dir') or ''), name, MAX_ARTIFACT_BYTES)
    content_type = 'application/json' if name.endswith('.json') else 'text/plain; charset=utf-8'
    return data, content_type


def refresh_activity_record(activity_id, registry):
    """Existing background/lazy activity refresh uses the same pinned evidence ladder."""
    activity = registry.get_activity(activity_id)
    if not isinstance(activity, dict):
        raise KeyError('Unknown modular ClusterKit activity')
    _, plan = load_intent(activity['log_dir'], activity_id)
    import host_exec
    roles, _ = host_exec.read_only_roles()
    context = execution_context.capture(plan['scripts_directory'], roles.select('control'))
    for role in ('scripts', 'logs'):
        execution_context.capture(plan['scripts_directory'], roles.select(role))
    require_activity_context(activity, context)
    from schedulers.slurm import SlurmScheduler
    scheduler = SlurmScheduler(execution_context.PinnedLocalConnection(context),
                               execution_context.PinnedPlatform(context))
    return reconcile(activity_id, registry, scheduler, context=context)
