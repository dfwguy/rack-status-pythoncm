# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Bounded normalization of one modular attempt; never changes vendor artifacts."""
from __future__ import annotations

import ipaddress
import itertools
import json
import math
from pathlib import Path

from clusterkit_metrics import get_cell


def read_json(path, limit=2 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlink artifacts are not accepted")
    with path.open('rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Artifact exceeds size limit")
    return json.loads(data)


def write_json(path, value):
    """Exclusive creation: even an interrupted prior attempt is never replaced."""
    with Path(path).open('x') as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write('\n')


def decode_pairs(raw, nodes, test, job_id):
    """Use the same upper triangle as the legacy API; count unordered pairs once."""
    if not isinstance(raw, dict) or raw.get('Testname') != test:
        raise ValueError('Unexpected ClusterKit test payload')
    mapping, links = raw.get('Nodes'), raw.get('Links')
    if (not isinstance(mapping, dict) or set(mapping) != set(nodes)
            or any(type(i) is not int for i in mapping.values())
            or sorted(mapping.values()) != list(range(len(nodes)))
            or not isinstance(links, list) or len(links) > len(nodes)):
        raise ValueError('ClusterKit node/rank matrix does not match the exact plan')
    if str(raw.get('JOBID')) != str(job_id):
        raise ValueError('ClusterKit job ID does not match this attempt')
    if raw.get('Units') != {'bandwidth': 'MB/s', 'latency': 'usec'}[test]:
        raise ValueError('Unrecognized metric units; values were not converted')
    for i, row in enumerate(links):
        if not isinstance(row, list) or len(row) > len(nodes) - i:
            raise ValueError('Malformed triangular matrix')
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)) for v in row):
            raise ValueError('Non-numeric/non-finite matrix value')
    pairs, missing, failed = [], [], []
    for a, b in itertools.combinations(nodes, 2):
        value = get_cell(links, mapping[a], mapping[b])
        if value is None:
            missing.append([a, b])
        elif not math.isfinite(value) or value <= 0:
            failed.append([a, b])
        else:
            pairs.append({'nodes': [a, b], 'value': value})
    return {'units': raw['Units'], 'pairs': pairs, 'observed_pair_count': len(pairs),
            'missing_pairs': missing, 'failed_pairs': failed,
            'vendor_metadata': {k: raw.get(k) for k in ('PPN', 'Technology', 'HCA_Tag', 'Bidirectional')}}


def metric_files(directory):
    """Bound depth, entry count and file count; never follow directory symlinks."""
    found, pending, entries = [], [(Path(directory), 0)], 0
    while pending:
        root, depth = pending.pop()
        if not root.exists():
            continue
        if root.is_symlink():
            raise ValueError('Symlink artifact directory')
        for child in root.iterdir():
            entries += 1
            if entries > 128:
                raise ValueError('Too many stage artifacts')
            if child.is_symlink():
                raise ValueError('Symlink artifact')
            if child.is_dir():
                if depth >= 3:
                    raise ValueError('Artifact nesting exceeds limit')
                pending.append((child, depth + 1))
            elif child.suffix == '.json':
                found.append(child)
    return sorted(found)


def preflight_evidence(plan, root, job_id):
    """Read every rank before any MPI test; no successful step alone is sufficient."""
    observations, errors, addresses = [], [], set()
    for rank in range(plan['node_count']):
        try:
            record = read_json(Path(root) / f'node-{rank}-before.json')
            profile = plan['effective_profile']
            gid = record['global_v2_gid']
            address = ipaddress.IPv6Address(gid['gid'])
            comparable = address.ipv4_mapped or address
            if (record.get('rank') != rank or record.get('host') not in plan['nodes']
                    or str(record.get('job_id')) != str(job_id) or record.get('status') != 'PASS'
                    or type(record.get('assigned_cpu')) is not int
                    or record.get('affinity_after') != [record.get('assigned_cpu')]
                    or record.get('architecture') != profile['expected_architecture']
                    or record.get('link_layer') != profile['expected_link_layer']
                    or (profile['hca'] and record.get('hca') != profile['hca'])
                    or type(record.get('gid_index')) is not int or record['gid_index'] < 0
                    or gid.get('index') != record['gid_index'] or gid.get('type') != 'RoCE v2'
                    or comparable.is_link_local or comparable.is_unspecified or comparable.is_multicast
                    or comparable in addresses):
                raise ValueError('Missing/invalid/duplicate verified preflight evidence')
            addresses.add(comparable)
            observations.append(record)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f'Preflight rank {rank}: {exc}')
    if len({r['host'] for r in observations}) != plan['node_count']:
        errors.append('Preflight did not cover every exact planned node once')
    return observations, errors


def process_failure(root, stage, nodes, exit_status):
    """Extract bounded primary evidence; a transport timeout is not a fabric diagnosis."""
    message = f'{stage}: process failed (exit {exit_status}); see {stage}.log'
    failure = {'category': 'PROCESS_FAILED', 'message': message, 'evidence': [], 'affected_nodes': []}
    path = Path(root) / f'{stage}.log'
    try:
        if path.is_symlink():
            return failure
        with path.open('rb') as handle:
            text = handle.read(128 * 1024).decode('utf-8', errors='replace')
    except OSError:
        return failure
    for number, line in enumerate(text.splitlines(), 1):
        if (('ud_ep.c' in line and 'Fatal:' in line and 'unhandled timeout error' in line)
                or ('UCX' in line and 'Endpoint timeout' in line)):
            failure['category'] = 'UCX_COMMUNICATION_TIMEOUT'
            if len(failure['evidence']) < 4:
                failure['evidence'].append({'artifact': f'{stage}.log', 'line': number, 'excerpt': line[:512]})
            host = line.split(']', 1)[0].lstrip('[').split(':', 1)[0]
            if host in nodes and host not in failure['affected_nodes']:
                failure['affected_nodes'].append(host)
    if failure['category'] == 'UCX_COMMUNICATION_TIMEOUT':
        phase = 'MPI setup' if 'PMPI_Comm_split_type' in text else 'MPI communication'
        failure['message'] = f'{stage}: UCX communication timed out during {phase} (exit {exit_status}); see {stage}.log'
    return failure


def summarize(plan, root, outcome):
    """No functional PASS from exit=0 alone: require evidence and full coverage."""
    root = Path(root)
    nodes = plan['nodes']
    errors, warnings, observations, stages = list(outcome.get('errors', [])), list(plan['warnings']), [], {}
    job_id = outcome.get('job_id')
    observations, preflight_errors = preflight_evidence(plan, root, job_id)
    errors.extend(preflight_errors)
    preflight_ok = not preflight_errors and outcome.get('preflight_exit') == 0
    required_stages = [] if plan['scope'] == 'preflight' else ['baseline'] + (
        ['single-path'] if plan['request']['path_comparison'] is True or (
            plan['request']['path_comparison'] is None and plan['effective_profile']['path_comparison']) else [])
    prerequisite = None if preflight_ok else 'preflight did not complete successfully'
    if not preflight_ok and not preflight_errors:
        errors.append('Preflight did not complete successfully')
    exits = outcome.get('stages', {})
    for stage in required_stages:
        if stage not in exits:
            reason = (f'Not run because {prerequisite}' if prerequisite else
                      'Planned stage has no execution record')
            stages[stage] = {'status': 'SKIPPED' if prerequisite else 'FAIL',
                             'reason': reason, 'exit_status': None, 'metrics': {}, 'placements': []}
            if prerequisite:
                warnings.append(f'{stage}: {reason}')
            else:
                errors.append(f'{stage}: {reason}')
                prerequisite = f'{stage} did not run'
            continue
        artifact_errors = []
        result = {'status': 'PASS' if type(exits[stage]) is int and exits[stage] == 0 else 'FAIL',
                  'exit_status': exits[stage],
                  'metrics': {test: {'units': units, 'pairs': [], 'observed_pair_count': 0,
                                     'missing_pairs': plan['expected_pairs'], 'failed_pairs': [], 'source': None}
                              for test, units in (('bandwidth', 'MB/s'), ('latency', 'usec'))},
                  'placements': []}
        stages[stage] = result
        if result['status'] == 'FAIL':
            result['failure'] = process_failure(root, stage, nodes, result['exit_status'])
            errors.append(result['failure']['message'])
            prerequisite = f'{stage} failed'

        for rank in range(len(nodes)):
            try:
                placement = read_json(root / f'rank-placement-{stage}-{rank}.json')
                if not isinstance(placement, dict):
                    raise TypeError('Placement must be an object')
                before = next(r for r in observations if r['rank'] == rank)
                if (placement.get('rank') != rank or placement.get('stage') != stage
                        or placement.get('host') != before['host']
                        or str(placement.get('job_id')) != str(job_id)
                        or placement.get('hca') != before['hca']
                        or placement.get('gid_index') != before['gid_index']
                        or placement.get('affinity_after') != before['affinity_after']):
                    raise ValueError('Placement differs from verified preflight')
                result['placements'].append(placement)
            except (OSError, ValueError, TypeError, StopIteration) as exc:
                artifact_errors.append(f'{stage} rank {rank}: {exc}')
        payloads = {}
        try:
            for path in metric_files(root / stage):
                raw = read_json(path)
                if not isinstance(raw, dict):
                    raise TypeError('Non-object vendor JSON')
                test = raw.get('Testname')
                if test not in ('bandwidth', 'latency'):
                    continue
                if test in payloads:
                    raise ValueError('Duplicate test payload; coverage is ambiguous')
                payloads[test] = (path, raw)
            for test in ('bandwidth', 'latency'):
                if test not in payloads:
                    raise ValueError(f'Missing {test} JSON')
                path, raw = payloads[test]
                metrics = decode_pairs(raw, nodes, test, job_id)
                metrics['source'] = str(path.relative_to(root))
                result['metrics'][test] = metrics
                if metrics['missing_pairs'] or metrics['failed_pairs']:
                    artifact_errors.append(f'{stage} {test}: incomplete/failed pair coverage')
        except (OSError, ValueError, TypeError) as exc:
            artifact_errors.append(f'{stage}: {exc}')
        if artifact_errors:
            result['evidence_errors'] = artifact_errors
            # Preserve secondary evidence details without burying the primary process failure.
            if result['status'] == 'PASS':
                result['status'] = 'FAIL'
                errors.extend(artifact_errors)
    if outcome.get('primary_exit') != 0 and not errors:
        errors.append('Primary attempt failed; diagnostics cannot change its result')
    diagnostic_warning = outcome.get('diagnostics', {}).get('after_exit') not in (None, 0)
    if diagnostic_warning:
        warnings.append('Post-test diagnostics failed; primary result and raw output are preserved.')
    warnings.extend(['Vendor PPN/Technology are preserved as vendor metadata, not actual rank count or link capacity.',
                     'Raw bandwidth/latency are measurements, not performance acceptance.'])
    return {'schema_version': 1, 'attempt_id': plan['attempt_id'], 'started_at': outcome.get('started_at'),
            'finished_at': outcome.get('finished_at'), 'nodes': nodes, 'slurm_job_id': job_id,
            'slurm_state': 'UNKNOWN', 'scheduler_reconciliation_required': True,
            'effective_configuration': plan['effective_profile'], 'plan_hash': plan['plan_hash'],
            'preflight': {'status': 'PASS' if preflight_ok else 'FAIL', 'nodes': observations,
                          'exit_status': outcome.get('preflight_exit')},
            'clusterkit_exit_status': outcome.get('stages', {}), 'primary_exit_status': outcome.get('primary_exit'),
            'expected_pairs': plan['expected_pairs'], 'expected_pair_count': plan['expected_pair_count'],
            'coverage_claim': 'preflight-only' if not required_stages else 'observed-unordered-pairs-per-metric',
            'stages': stages, 'diagnostics': outcome.get('diagnostics', {}), 'errors': errors,
            'warnings': warnings, 'functional_status': 'FAIL' if errors else ('WARN' if diagnostic_warning else 'PASS'),
            'performance_status': 'UNVERIFIED'}
