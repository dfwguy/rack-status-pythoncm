# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Validate normalized identities and evidence before accepting functional success."""
import ipaddress
import math


def validate_summary(summary, plan, job_id):
    if not isinstance(summary, dict) or type(summary.get('schema_version')) is not int or summary['schema_version'] != 1:
        raise ValueError('Missing normalized summary schema')
    identity = {'attempt_id': plan['attempt_id'], 'plan_hash': plan['plan_hash'],
                'nodes': plan['nodes'], 'effective_configuration': plan['effective_profile'],
                'expected_pairs': plan['expected_pairs'], 'expected_pair_count': plan['expected_pair_count']}
    if type(summary.get('expected_pair_count')) is not int or any(summary.get(key) != value for key, value in identity.items()):
        raise ValueError('Summary identity/configuration/coverage does not match this attempt')
    if str(summary.get('slurm_job_id')) != str(job_id):
        raise ValueError('Summary Slurm job ID does not match the submission receipt')
    if summary.get('functional_status') not in {'PASS', 'WARN', 'FAIL'}:
        raise ValueError('Invalid functional status')
    if summary.get('performance_status') != 'UNVERIFIED':
        raise ValueError('Performance acceptance is not supported by this execution recipe')
    if summary.get('coverage_claim') != ('preflight-only' if plan['scope'] == 'preflight' else 'observed-unordered-pairs-per-metric'):
        raise ValueError('Summary coverage claim does not match its scope')
    if summary['functional_status'] == 'FAIL':
        return summary
    if summary.get('errors') != [] or type(summary.get('primary_exit_status')) is not int or summary['primary_exit_status'] != 0:
        raise ValueError('Passing summary lacks successful primary evidence')
    preflight = summary.get('preflight')
    if not isinstance(preflight, dict) or preflight.get('status') != 'PASS' or type(preflight.get('exit_status')) is not int or preflight['exit_status'] != 0:
        raise ValueError('Passing summary lacks a verified preflight')
    observations = preflight.get('nodes')
    if not isinstance(observations, list) or len(observations) != plan['node_count']:
        raise ValueError('Preflight does not cover all planned nodes')
    by_rank, hosts, addresses = {}, set(), set()
    profile = plan['effective_profile']
    for record in observations:
        if not isinstance(record, dict):
            raise ValueError('Malformed preflight record')  # noqa: TRY004 - Invalid serialized evidence.
        rank, host = record.get('rank'), record.get('host')
        if (type(rank) is not int or rank not in range(plan['node_count']) or rank in by_rank
                or host not in plan['nodes'] or host in hosts or record.get('status') != 'PASS'
                or str(record.get('job_id')) != str(job_id)
                or record.get('architecture') != profile['expected_architecture']
                or record.get('link_layer') != profile['expected_link_layer']
                or (profile['hca'] and record.get('hca') != profile['hca'])
                or type(record.get('assigned_cpu')) is not int
                or record.get('assigned_cpu') < 0
                or record.get('affinity_after') != [record['assigned_cpu']]
                or type(record.get('gid_index')) is not int or record['gid_index'] < 0):
            raise ValueError('Invalid/duplicate preflight placement or network identity')
        gid = record.get('global_v2_gid')
        if not isinstance(gid, dict) or gid.get('type') != 'RoCE v2' or gid.get('index') != record['gid_index']:
            raise ValueError('Preflight lacks matching per-node global RoCE-v2 GID')
        address = ipaddress.IPv6Address(gid.get('gid'))
        address = address.ipv4_mapped or address
        if address.is_link_local or address.is_unspecified or address.is_multicast or address in addresses:
            raise ValueError('Invalid/duplicate preflight GID address')
        by_rank[rank] = record
        hosts.add(host)
        addresses.add(address)
    compare = plan['request']['path_comparison']
    if compare is None:
        compare = profile['path_comparison']
    required = [] if plan['scope'] == 'preflight' else ['baseline'] + (['single-path'] if compare else [])
    stages = summary.get('stages')
    exits = summary.get('clusterkit_exit_status')
    if not isinstance(stages, dict) or set(stages) != set(required) or not isinstance(exits, dict) or set(exits) != set(required):
        raise ValueError('Missing or unexpected execution stages')
    expected = {frozenset(pair) for pair in plan['expected_pairs']}
    for stage in required:
        result = stages[stage]
        if (type(exits[stage]) is not int or exits[stage] != 0 or not isinstance(result, dict)
                or type(result.get('exit_status')) is not int or result['exit_status'] != 0
                or result.get('status', 'PASS') != 'PASS'):
            raise ValueError('Passing summary has a failed stage')
        placements = result.get('placements')
        if not isinstance(placements, list) or len(placements) != plan['node_count']:
            raise ValueError('Missing per-rank stage placement')
        ranks = set()
        for placement in placements:
            if not isinstance(placement, dict) or type(placement.get('rank')) is not int:
                raise ValueError('Invalid rank placement')
            rank = placement['rank']
            if rank not in by_rank or rank in ranks or placement.get('stage') != stage:
                raise ValueError('Unexpected/duplicate rank placement')
            before = by_rank[rank]
            for key in ('host', 'job_id', 'hca', 'gid_index', 'affinity_after'):
                if placement.get(key) != before.get(key):
                    raise ValueError('Stage placement differs from preflight')
            ranks.add(rank)
        metrics = result.get('metrics')
        if not isinstance(metrics, dict) or set(metrics) != {'bandwidth', 'latency'}:
            raise ValueError('Missing pairwise metrics')
        for metric, units in (('bandwidth', 'MB/s'), ('latency', 'usec')):
            data = metrics[metric]
            if (not isinstance(data, dict) or data.get('units') != units
                    or data.get('missing_pairs') != [] or data.get('failed_pairs') != []
                    or type(data.get('observed_pair_count')) is not int
                    or data['observed_pair_count'] != len(expected)
                    or not isinstance(data.get('pairs'), list) or len(data['pairs']) != len(expected)):
                raise ValueError('Incomplete pair coverage')
            seen = set()
            for pair in data['pairs']:
                if not isinstance(pair, dict) or not isinstance(pair.get('nodes'), list) or len(pair['nodes']) != 2 or any(not isinstance(n, str) for n in pair['nodes']):
                    raise ValueError('Invalid observed pair')
                key = frozenset(pair['nodes'])
                value = pair.get('value')
                if key not in expected or key in seen or type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError('Missing/duplicate/failed observed pair')
                seen.add(key)
    return summary
