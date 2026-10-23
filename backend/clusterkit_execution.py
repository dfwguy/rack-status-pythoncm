# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Reviewed compute-side execution and explicit local attempt preparation.

No scheduler submission, cancellation, SSH, job registry or API is implemented
here. Phase 4 must use the existing workflow/activity services for those concerns.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from clusterkit_planner import PlanningError, build_plan
from clusterkit_profile import parse_profile
from clusterkit_results import preflight_evidence, read_json, summarize, write_json

# Healthy multi-rail UCX discovery can exceed five seconds. This remains
# within the existing 45-second one-node / 60-second pairwise preflight step.
UCX_DEVICE_QUERY_TIMEOUT_SECONDS = 15

SOURCE_FILES = ('backend/clusterkit_profile.py', 'backend/clusterkit_planner.py', 'backend/clusterkit_job.py',
                'backend/clusterkit_metrics.py', 'backend/clusterkit_results.py',
                'backend/schedulers/__init__.py', 'backend/schedulers/base.py',
                'backend/schedulers/slurm.py', 'backend/platforms/base.py',
                'backend/clusterkit_execution.py', 'bin/scripts/clusterkit_execute.py',
                'bin/scripts/clusterkit_preflight.sbatch', 'bin/scripts/clusterkit_pairwise.sbatch',
                'bin/scripts/clusterkit_rank.sh', 'bin/scripts/clusterkit_suite_node_diag.py')


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def source_path(name, scripts_dir):
    """Backend code belongs to the app; script bytes belong to the selected layer."""
    path = Path(name)
    if path.parts[0] == 'backend':
        return Path(__file__).resolve().parent.joinpath(*path.parts[1:])
    if path.parts[:2] == ('bin', 'scripts'):
        return Path(scripts_dir).resolve().joinpath(*path.parts[2:])
    raise ValueError('Unknown reviewed source: ' + name)


def validate_plan(plan, scripts_dir):
    """Reconstruct every argument from validated policy; a hash is not authority."""
    try:
        plan = json.loads(json.dumps(plan, allow_nan=False))
        if plan['schema_version'] != 2 or plan['scripts_directory'] != str(Path(scripts_dir).resolve()):
            raise ValueError('Plan version or reviewed script location differs')
        resolution = parse_profile(plan['effective_profile'])
        rebuilt = build_plan(plan['request'], resolution, plan['inventory_snapshot'],
                             scripts_dir=plan['scripts_directory'], attempt_id=plan['attempt_id'])
        if json.dumps(plan, sort_keys=True) != json.dumps(rebuilt, sort_keys=True):
            raise ValueError('Plan differs from the validated policy/request')
        if not plan['entrypoint_supported'] or not plan['sbatch_argv']:
            raise ValueError('Execution recipe is unsupported: ' + '; '.join(plan['execution_blockers']))
        if len({n.split('.')[0].casefold() for n in plan['nodes']}) != len(plan['nodes']):
            raise ValueError('Ambiguous short node names')
        return plan
    except (KeyError, TypeError, ValueError, PlanningError) as exc:
        raise ValueError(f'Invalid execution plan: {exc}') from exc


def prepare_attempt(plan, scripts_dir):
    """Administrator-only staging, NEVER submission. Refuse any existing attempt."""
    plan = validate_plan(plan, scripts_dir)
    scripts = Path(scripts_dir).resolve()
    root = Path(plan['results_directory'])
    parent = Path(plan['effective_profile']['results_root'])
    if not parent.is_dir():
        raise ValueError('Configured results root must already exist')
    if root.parent != parent:
        raise ValueError('Attempt is outside the configured results root')
    # All reads/preconditions first; failure after mkdir deliberately retains evidence.
    sources = {name: source_path(name, scripts).read_bytes() for name in SOURCE_FILES}
    init = Path(plan['effective_profile']['hpcx_init'])
    init_hash = digest(init)
    root.mkdir(mode=0o750)  # exclusive even for an empty prior/failed attempt
    write_json(root / 'plan.json', plan)
    manifest = {'schema_version': 1, 'plan_hash': plan['plan_hash'], 'prepared_at': now(),
                'hpcx_init_sha256': init_hash,
                'sources': {n: hashlib.sha256(data).hexdigest() for n, data in sources.items()}}
    for name, data in sources.items():
        path = root / 'source' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as handle:
            handle.write(data)
    write_json(root / 'source-manifest.json', manifest)
    return {'prepared': True, 'submitted': False, 'results_directory': str(root),
            'sbatch_argv': plan['sbatch_argv'], 'command_display': plan['command_display'],
            'warning': 'Staging is not authorization. No scheduler job was submitted.'}


def load_attempt(plan_path, scripts_dir):
    plan = validate_plan(read_json(plan_path), scripts_dir)
    root = Path(plan['results_directory'])
    if Path(plan_path).resolve() != (root / 'plan.json').resolve() or root.is_symlink():
        raise ValueError('Plan must be the prepared attempt plan.json')
    manifest = read_json(root / 'source-manifest.json')
    if manifest.get('plan_hash') != plan['plan_hash'] or set(manifest.get('sources', {})) != set(SOURCE_FILES):
        raise ValueError('Incomplete or mismatched source manifest')
    for name, sha in manifest['sources'].items():
        if digest(source_path(name, scripts_dir)) != sha or digest(root / 'source' / name) != sha:
            raise ValueError('Reviewed source changed after preparation: ' + name)
    if digest(plan['effective_profile']['hpcx_init']) != manifest.get('hpcx_init_sha256'):
        raise ValueError('HPC-X init changed after preparation')
    return plan


def checked_output(argv, seconds=5, include_stderr=False):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=seconds, check=False)
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        raise RuntimeError('Required read-only command failed: ' + argv[0])
    return result.stdout + (result.stderr if include_stderr else '')


def allocation(plan):
    job = os.environ.get('SLURM_JOB_ID', '')
    count = plan['node_count']
    if (not job.isdecimal() or os.environ.get('SLURM_JOB_NUM_NODES') != str(count)
            or os.environ.get('SLURM_NTASKS') != str(count)
            or os.environ.get('SLURM_CPUS_PER_TASK') != '1'
            or os.environ.get('SLURM_JOB_PARTITION') != plan['effective_profile']['partition']
            or os.environ.get('SLURM_JOB_GPUS', '')):
        raise RuntimeError('Requires the exact planned CPU-only Slurm allocation')
    hosts = checked_output(['scontrol', 'show', 'hostnames', os.environ.get('SLURM_JOB_NODELIST', '')]).splitlines()
    if len(hosts) != count or set(hosts) != set(plan['nodes']):
        raise RuntimeError('Allocation differs from the exact planned nodes')
    return job


def legacy_diag(scripts_dir):
    # Reuse proven global-GID selection and verified Slurm CPU mapping unchanged.
    spec = importlib.util.spec_from_file_location('_clusterkit_proven_diag', Path(scripts_dir) / 'clusterkit_suite_node_diag.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rank_context(plan):
    value = os.environ.get('SLURM_PROCID', '')
    if not value.isdecimal() or not 0 <= int(value) < plan['node_count'] or os.environ.get('SLURM_LOCALID') != '0':
        raise RuntimeError('Requires one Slurm rank per allocated node')
    host = socket.gethostname()
    matches = [n for n in plan['nodes'] if n == host or n.split('.')[0] == host.split('.')[0]]
    if len(matches) != 1:
        raise RuntimeError('This rank is not on exactly one planned node')
    return int(value), matches[0]


def choose_hca(profile, diag, sysfs=Path('/sys/class/infiniband')):
    """Discovery succeeds only when exactly one active matching port is usable."""
    if profile['hca_policy'] == 'configured' or profile['hca']:
        candidates = [profile['hca']]
    else:
        candidates = []
        for dev in sorted(sysfs.iterdir()):
            for port in sorted((dev / 'ports').iterdir()):
                if port.name.isdecimal():
                    candidates.append(dev.name + ':' + port.name)
                    if len(candidates) > 128:
                        raise RuntimeError('RDMA discovery exceeds port limit')
    usable, failures = [], []
    for hca in candidates:
        try:
            dev, number = hca.split(':')
            port = sysfs / dev / 'ports' / number
            if (not re.fullmatch(r'4:\s*ACTIVE', (port / 'state').read_text().strip())
                    or not re.fullmatch(r'5:\s*LinkUp', (port / 'phys_state').read_text().strip())
                    or (port / 'link_layer').read_text().strip() != profile['expected_link_layer']):
                raise RuntimeError('Port is not active/link-up with the expected link layer')
            record = diag.observation(hca)
            record['link_layer'] = (port / 'link_layer').read_text().strip()
            record['physical_state'] = (port / 'phys_state').read_text().strip()
            usable.append(record)
        except (OSError, ValueError, RuntimeError) as exc:
            failures.append({'hca': hca, 'error': str(exc)})
    if len(usable) != 1:
        raise RuntimeError(f'Expected one unambiguous usable RDMA port, found {len(usable)}; {failures}')
    return usable[0]


def bind_cpu(diag):
    cpu = diag.allocated_cpu()
    before = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    after = sorted(os.sched_getaffinity(0))
    if after != [cpu]:
        raise RuntimeError('CPU affinity does not match the assigned Slurm CPU')
    return {'assigned_cpu': cpu, 'affinity_before': before, 'affinity_after': after}


def tool_paths():
    paths = {key: os.environ.get(key, '') for key in ('HPCX_DIR', 'HPCX_MPI_DIR', 'HPCX_UCX_DIR')}
    if any(not Path(p).is_absolute() for p in paths.values()):
        raise RuntimeError('HPC-X did not resolve absolute tool roots')
    tools = {'clusterkit': str(Path(paths['HPCX_DIR']) / 'clusterkit/bin/clusterkit'),
             'mpi': str(Path(paths['HPCX_MPI_DIR']) / 'bin/mpirun'),
             'ucx': str(Path(paths['HPCX_UCX_DIR']) / 'bin/ucx_info')}
    if any(not Path(p).is_file() or not os.access(p, os.X_OK) for p in tools.values()):
        raise RuntimeError('Required ClusterKit/MPI/UCX executable is missing')
    return tools


def preflight_node(plan, scripts_dir):
    rank, host = rank_context(plan)
    profile, root = plan['effective_profile'], Path(plan['results_directory'])
    record = {'rank': rank, 'host': host, 'job_id': os.environ['SLURM_JOB_ID'], 'at': now(), 'status': 'FAIL'}
    try:
        if platform.machine() != profile['expected_architecture']:
            raise RuntimeError('Unexpected compute architecture: ' + platform.machine())
        diag = legacy_diag(scripts_dir)
        network = choose_hca(profile, diag)
        # Normalize host to the exact Slurm name; retain observed hostname separately.
        network['observed_hostname'] = network.pop('host')
        record.update(network)
        record.update(bind_cpu(diag))
        tools = tool_paths()
        record['tools'] = tools
        record['mpi_version'] = checked_output([tools['mpi'], '--version'])[:4096]
        record['ucx_version'] = checked_output([tools['ucx'], '-v'])[:4096]
        record['ucx_device_inventory_timeout_seconds'] = UCX_DEVICE_QUERY_TIMEOUT_SECONDS
        started = time.monotonic()
        try:
            ucx = checked_output([tools['ucx'], '-d', '-t', 'rc_mlx5'],
                                 seconds=UCX_DEVICE_QUERY_TIMEOUT_SECONDS)
        finally:
            record['ucx_device_inventory_seconds'] = round(time.monotonic() - started, 3)
        if not re.search(r'Device:\s*' + re.escape(record['hca']) + r'(?:\s|$)', ucx):
            raise RuntimeError('Selected HCA is absent from the HPC-X UCX rc inventory')
        record['ucx_device_inventory'] = ucx
        record['gid_index'] = record['global_v2_gid']['index']
        record['architecture'] = platform.machine()
        record['status'] = 'PASS'
    except Exception as exc:
        record['error'] = str(exc)
        raise
    finally:
        write_json(root / f'node-{rank}-before.json', record)


def rank_exec(plan, scripts_dir, stage):
    if plan['scope'] == 'preflight' or stage not in ('baseline', 'single-path'):
        raise RuntimeError('No ClusterKit invocation is permitted for this scope/stage')
    compare = plan['request']['path_comparison']
    if compare is None:
        compare = plan['effective_profile']['path_comparison']
    if stage == 'single-path' and not compare:
        raise RuntimeError('Unplanned path comparison')
    rank, host = rank_context(plan)
    root, profile = Path(plan['results_directory']), plan['effective_profile']
    before = read_json(root / f'node-{rank}-before.json')
    if before.get('status') != 'PASS' or before.get('host') != host or before.get('job_id') != os.environ['SLURM_JOB_ID']:
        raise RuntimeError('This rank has no matching successful preflight')
    diag = legacy_diag(scripts_dir)
    current = choose_hca(dict(profile, hca_policy='configured', hca=before['hca']), diag)
    if current['global_v2_gid'] != before['global_v2_gid']:
        raise RuntimeError('Selected GID changed since preflight')
    binding = bind_cpu(diag)
    if binding['affinity_after'] != before['affinity_after']:
        raise RuntimeError('Assigned CPU changed since preflight')
    tools = tool_paths()
    if tools != before['tools']:
        raise RuntimeError('HPC-X tool paths changed since preflight')
    os.environ.update({'UCX_NET_DEVICES': before['hca'], 'UCX_IB_GID_INDEX': str(before['gid_index']),
                       'UCX_TLS': ','.join(profile['ucx_transports']), 'UCX_VFS_ENABLE': 'n',
                       'UCX_RC_MLX5_NUM_PATHS': '1' if stage == 'single-path' else 'auto',
                       'UCX_LOG_LEVEL': 'info', 'UCX_LOG_FILE': str(root / f'ucx-{stage}-%h-%p.log'),
                       'UCX_LOG_FILE_SIZE': '16mb', 'UCX_LOG_FILE_ROTATE': '1',
                       'OMPI_MCA_pml': 'ucx', 'OMPI_MCA_coll_hcoll_enable': '0', 'OMPI_MCA_coll_ucc_enable': '0',
                       'OMPI_MCA_hwloc_base_binding_policy': 'none', 'HCA_TAG': before['hca'],
                       'CUDA_VISIBLE_DEVICES': ''})
    os.environ.pop('CK_OUTPUT_SUBDIR', None)
    placement = {'rank': rank, 'host': host, 'stage': stage, 'job_id': os.environ['SLURM_JOB_ID'],
                 'hca': before['hca'], 'gid_index': before['gid_index'], 'gid': before['global_v2_gid'],
                 'ucx_transports': profile['ucx_transports'], 'ucx_rc_paths': os.environ['UCX_RC_MLX5_NUM_PATHS'],
                 **binding}
    write_json(root / f'rank-placement-{stage}-{rank}.json', placement)
    params = profile['functional']
    argv = [tools['clusterkit'], '-d', 'bw', '-d', 'lat',
            '--biters=' + str(params['bandwidth_iterations']), '--bsize=' + str(params['bandwidth_bytes']),
            '--liters=' + str(params['latency_iterations']), '--lsize=' + str(params['latency_bytes']),
            '--disable-retest', '--skip_intra_node', '--unidirectional',
            '--time-limit=' + str(params['test_limit_seconds']), '--outputdir=' + str(root / stage)]
    print(json.dumps({'rank': rank, 'host': host, 'stage': stage, 'argv': argv}), flush=True)
    os.execv(argv[0], argv)


def clean_environment(environ):
    env = {k: v for k, v in environ.items() if not k.startswith(('UCX_', 'OMPI_MCA_', 'CK_')) and k != 'HCA_TAG'}
    env.update({'HPCX_ENABLE_NCCL_MRC_PLUGIN': '0', 'HPCX_ENABLE_NCCL_SPCX_PLUGIN': '0',
                'CUDA_VISIBLE_DEVICES': '', 'PYTHONDONTWRITEBYTECODE': '1'})
    return env


def launch_node(plan, plan_path, scripts_dir, stage):
    # Argument array and reviewed static shell only; no generated shell, eval or SSH.
    rank_context(plan)
    os.execve('/bin/bash', ['bash', str(Path(scripts_dir) / 'clusterkit_rank.sh'),
                           str(plan_path), stage, str(scripts_dir)], clean_environment(os.environ))


def diagnostic_node(plan, scripts_dir):
    rank, host = rank_context(plan)
    root = Path(plan['results_directory'])
    before = read_json(root / f'node-{rank}-before.json')
    record = legacy_diag(scripts_dir).observation(before['hca'])
    record.update({'rank': rank, 'host': host, 'job_id': os.environ['SLURM_JOB_ID'], 'at': now()})
    write_json(root / f'node-{rank}-after.json', record)


def run_step(argv, logfile, seconds):
    """Timeout the whole new process group; reap this step, never cancel other jobs."""
    with Path(logfile).open('xb') as handle:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                   start_new_session=True)

        def terminate():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        try:
            return process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            terminate()
            return 124
        except BaseException:
            terminate()
            raise


def batch(plan, plan_path, scripts_dir, kind):
    if (kind == 'preflight') != (plan['scope'] == 'preflight'):
        raise RuntimeError('Static entrypoint does not match the planned scope')
    root = Path(plan['results_directory'])
    # A second launch of the same prepared attempt cannot overwrite any evidence.
    with (root / 'execution-claim.json').open('x') as claim:
        json.dump({'at': now(), 'job_id': os.environ.get('SLURM_JOB_ID')}, claim)
    started = time.monotonic()
    outcome = {'job_id': os.environ.get('SLURM_JOB_ID'), 'started_at': now(), 'preflight_exit': None,
               'primary_exit': 1, 'stages': {}, 'diagnostics': {}, 'errors': []}
    n = str(plan['node_count'])
    common = ['srun', '--nodes=' + n, '--ntasks=' + n, '--ntasks-per-node=1', '--cpus-per-task=1',
              '--mem=1G', '--gres=none', '--exact', '--cpu-bind=cores', '--kill-on-bad-exit=1',
              '--nodelist=' + ','.join(plan['nodes'])]
    script = str(Path(scripts_dir) / 'clusterkit_execute.py')

    def step(phase, mpi, timeout):
        # Allow up to ten seconds to reap a timed-out step, plus final artifacts.
        remaining = plan['resources']['wall_seconds'] - (time.monotonic() - started) - 25
        if remaining < 1:
            return 124
        return run_step([*common, '--mpi=' + mpi, 'python3', script, 'node-launch',
                         '--plan', str(plan_path), '--stage', phase], root / (phase + '.log'), min(timeout, remaining))

    def interrupted(signum, frame):
        raise RuntimeError(f'Batch interrupted by signal {signum}; no retry')
    old_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        write_json(root / 'allocation-environment.json', {
            key: os.environ.get(key) for key in ('SLURM_JOB_ID', 'SLURM_JOB_NODELIST',
            'SLURM_JOB_NUM_NODES', 'SLURM_NTASKS', 'SLURM_CPUS_PER_TASK',
            'SLURM_JOB_PARTITION', 'SLURM_JOB_GPUS')})
        outcome['job_id'] = allocation(plan)
        details = checked_output(['scontrol', 'show', 'job', '-d', outcome['job_id']])
        with (root / 'allocation.txt').open('x') as handle:
            handle.write(details)
        plugins = checked_output(['srun', '--mpi=list'], include_stderr=True)
        with (root / 'slurm-mpi-plugins.txt').open('x') as handle:
            handle.write(plugins)
        if not re.search(r'\bpmix_v3\b', plugins):
            raise RuntimeError('The reviewed PMIx v3 Slurm launch plugin is unavailable')
        os.environ['SLURM_EXPORT_ENV'] = 'ALL'
        outcome['preflight_exit'] = step('before', 'none', 45 if kind == 'preflight' else 60)
        outcome['primary_exit'] = outcome['preflight_exit']
        if outcome['preflight_exit'] == 0:
            _, errors = preflight_evidence(plan, root, outcome['job_id'])
            if errors:
                outcome['errors'].extend(errors)
                outcome['preflight_exit'] = outcome['primary_exit'] = 1
        if outcome['preflight_exit'] == 0 and kind != 'preflight':
            compare = plan['request']['path_comparison']
            if compare is None:
                compare = plan['effective_profile']['path_comparison']
            for stage in ['baseline'] + (['single-path'] if compare else []):
                rc = step(stage, 'pmix_v3', 2 * plan['effective_profile']['functional']['test_limit_seconds'] + 30)
                outcome['stages'][stage] = rc
                if rc:
                    outcome['primary_exit'] = rc
                    break  # no automatic retries or fallback benchmark
        if outcome['preflight_exit'] == 0:
            outcome['diagnostics']['after_exit'] = step('after', 'none', 15)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        outcome['errors'].append(str(exc))
        if outcome['primary_exit'] == 0:
            outcome['primary_exit'] = 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
        outcome['finished_at'] = now()
        write_json(root / 'outcome.json', outcome)
        summary = summarize(plan, root, outcome)
        write_json(root / 'summary.json', summary)
    return outcome['primary_exit'] or (0 if summary['functional_status'] in ('PASS', 'WARN') else 1)
