# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Offline execution simulations. No real Slurm, MPI, RDMA, CPU pinning or benchmarks."""
import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clusterkit_execution as E
from clusterkit_planner import build_plan, parse_inventory
from clusterkit_profile import parse_profile
from clusterkit_results import decode_pairs, read_json, summarize, write_json

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / 'bin/scripts'


def make_plan(root, count=2, compare=False, **changes):
    nodes = [f'node{i}' for i in range(count)]
    profile = {'enabled': True, 'partition': 'testq', 'hpcx_init': str(root / 'hpcx-init.sh'),
               'results_root': str(root), 'allowed_node_counts': [2, 4, 8], 'max_nodes': 8,
               'expected_architecture': 'aarch64', 'expected_link_layer': 'Ethernet',
               'hca': 'fake_rail:1', 'node_allowlist': nodes, 'path_comparison': compare}
    profile.update(changes)
    resolution = parse_profile(profile)
    assert resolution.ready, resolution.errors
    snapshot = parse_inventory('\n'.join(f'{n}|idle|testq' for n in nodes), nodes)
    return build_plan({'scope': 'preflight' if count == 1 else 'explicit', 'nodes': nodes},
                      resolution, snapshot, scripts_dir=str(SCRIPTS))


def node_record(plan, rank):
    return {'host': plan['nodes'][rank], 'rank': rank, 'status': 'PASS', 'job_id': '123',
            'hca': 'fake_rail:1', 'gid_index': 3 + rank * 2, 'assigned_cpu': rank,
            'architecture': 'aarch64', 'link_layer': 'Ethernet',
            'affinity_after': [rank], 'global_v2_gid': {'index': 3 + rank * 2, 'gid': f'2001:db8::{rank+1}', 'type': 'RoCE v2'},
            'tools': {'clusterkit': '/fake/clusterkit', 'mpi': '/fake/mpirun', 'ucx': '/fake/ucx_info'}}


def payload(plan, test):
    n = len(plan['nodes'])
    # Match vendor compact upper triangle, omitting the last all-diagonal row.
    return {'Nodes': {name: i for i, name in enumerate(reversed(plan['nodes']))},
            'Testname': test, 'JOBID': 123, 'Units': 'MB/s' if test == 'bandwidth' else 'usec',
            'PPN': 144, 'Technology': 'Unknown',
            'Links': [[0] + [float(100 + j) for j in range(n-i-1)] for i in range(n-1)]}


def write_before(plan, root):
    for rank in range(plan['node_count']):
        write_json(root / f'node-{rank}-before.json', node_record(plan, rank))


def write_stage(plan, root, stage):
    directory = root / stage / 'vendor-date';directory.mkdir(parents=True)
    for test in ('bandwidth', 'latency'):
        write_json(directory / (test + '.json'), payload(plan, test))
    for rank in range(plan['node_count']):
        record = node_record(plan, rank);record['stage'] = stage
        write_json(root / f'rank-placement-{stage}-{rank}.json', record)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'hpcx-init.sh').write_text('# fake init; never sources real HPC-X\n')

    def test_all_sizes_have_static_scope_specific_exact_plans(self):
        for count in (1, 2, 4, 8):
            plan = make_plan(self.root, count)
            self.assertEqual(E.validate_plan(plan, SCRIPTS)['node_count'], count)
            self.assertIn('--nodes=' + str(count), plan['sbatch_argv'])
            self.assertIn('--gres=none', plan['sbatch_argv'])
            self.assertNotIn('--exclusive', plan['sbatch_argv'])
            self.assertFalse(plan['submission_enabled'])
            self.assertEqual(plan['expected_pair_count'], count * (count-1)//2)

    def test_tampered_plans_cannot_change_commands_limits_or_output(self):
        plan = make_plan(self.root)
        for key, value in [('nodes', ['node0', 'outside']), ('node_count', 8),
                           ('sbatch_argv', ['touch', '/bad']), ('results_directory', '/bad'),
                           ('attempt_id', '../bad'), ('plan_hash', 'forged')]:
            changed = copy.deepcopy(plan);changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                E.validate_plan(changed, SCRIPTS)
        with self.assertRaises(ValueError): E.validate_plan(plan, '/another/scripts')

    def test_preparation_is_exclusive_and_preserves_reviewed_sources(self):
        plan = make_plan(self.root)
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('no process')), \
                mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('no submit')):
            result = E.prepare_attempt(plan, SCRIPTS)
            self.assertFalse(result['submitted'])
            root = Path(result['results_directory'])
            self.assertEqual(E.load_attempt(root / 'plan.json', SCRIPTS)['plan_hash'], plan['plan_hash'])
            with self.assertRaises(FileExistsError): E.prepare_attempt(plan, SCRIPTS)
            self.assertFalse((root / 'summary.json').exists())
            with (root / 'source/backend/clusterkit_profile.py').open('a') as handle:
                handle.write('# changed fixture\n')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                E.load_attempt(root / 'plan.json', SCRIPTS)

    def test_hpcx_changes_after_staging_fail_closed(self):
        plan = make_plan(self.root);E.prepare_attempt(plan, SCRIPTS)
        (self.root / 'hpcx-init.sh').write_text('# changed fake init\n')
        with self.assertRaisesRegex(ValueError, 'HPC-X init changed'):
            E.load_attempt(Path(plan['results_directory']) / 'plan.json', SCRIPTS)

    def test_exclusive_writer_never_replaces_results(self):
        path = self.root / 'summary.json';write_json(path, {'original': True})
        with self.assertRaises(FileExistsError): write_json(path, {'replacement': True})
        self.assertEqual(read_json(path), {'original': True})

    def test_allocation_requires_exact_set_count_cpu_partition_and_no_gpu(self):
        plan = make_plan(self.root, 4)
        env = {'SLURM_JOB_ID': '123', 'SLURM_JOB_NUM_NODES': '4', 'SLURM_NTASKS': '4',
               'SLURM_CPUS_PER_TASK': '1', 'SLURM_JOB_PARTITION': 'testq', 'SLURM_JOB_NODELIST': 'fixture'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(E, 'checked_output', return_value='\n'.join(reversed(plan['nodes']))):
            self.assertEqual(E.allocation(plan), '123')
            for key, value in [('SLURM_JOB_NUM_NODES', '2'), ('SLURM_NTASKS', '8'),
                               ('SLURM_CPUS_PER_TASK', '2'), ('SLURM_JOB_PARTITION', 'wrong'),
                               ('SLURM_JOB_GPUS', '0')]:
                with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}), self.assertRaises(RuntimeError):
                    E.allocation(plan)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(E, 'checked_output', return_value='node0\nnode1\nnode2\noutside'), \
                self.assertRaisesRegex(RuntimeError, 'exact planned nodes'):
            E.allocation(plan)

    def test_rank_context_supports_all_eight_and_rejects_extra_local_ranks(self):
        plan = make_plan(self.root, 8)
        for rank in range(8):
            with mock.patch.dict(os.environ, {'SLURM_PROCID': str(rank), 'SLURM_LOCALID': '0'}), \
                    mock.patch.object(E.socket, 'gethostname', return_value=f'node{rank}'):
                self.assertEqual(E.rank_context(plan), (rank, f'node{rank}'))
                with mock.patch.dict(os.environ, {'SLURM_LOCALID': '1'}), self.assertRaises(RuntimeError):
                    E.rank_context(plan)
        with mock.patch.dict(os.environ, {'SLURM_PROCID': '8', 'SLURM_LOCALID': '0'}), self.assertRaises(RuntimeError):
            E.rank_context(plan)

    def test_inherited_transport_overrides_are_removed_but_pmix_endpoints_remain(self):
        env = E.clean_environment({'UCX_IB_GID_INDEX': '999', 'UCX_TLS': 'tcp', 'OMPI_MCA_pml': 'ob1',
                                   'CK_OUTPUT_SUBDIR': '/bad', 'PMIX_SERVER_URI2': 'endpoint', 'PATH': '/bin'})
        self.assertNotIn('UCX_IB_GID_INDEX', env)
        self.assertNotIn('OMPI_MCA_pml', env)
        self.assertNotIn('CK_OUTPUT_SUBDIR', env)
        self.assertEqual(env['PMIX_SERVER_URI2'], 'endpoint')
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'], '')

    def test_configured_and_discovered_hca_fail_on_ambiguity_or_wrong_link(self):
        sysfs = self.root / 'sysfs'
        for dev in ('fake_rail', 'other_rail'):
            port = sysfs / dev / 'ports/1';port.mkdir(parents=True)
            for key, text in [('state', '4: ACTIVE'), ('phys_state', '5: LinkUp'), ('link_layer', 'Ethernet')]:
                (port / key).write_text(text)
        diag = mock.Mock();diag.observation.side_effect = lambda hca: {'hca': hca}
        profile = make_plan(self.root)['effective_profile']
        self.assertEqual(E.choose_hca(profile, diag, sysfs)['hca'], 'fake_rail:1')
        profile.update(hca_policy='discover', hca='')
        with self.assertRaisesRegex(RuntimeError, 'found 2'): E.choose_hca(profile, diag, sysfs)
        (sysfs / 'other_rail/ports/1/state').write_text('1: DOWN')
        self.assertEqual(E.choose_hca(profile, diag, sysfs)['hca'], 'fake_rail:1')
        (sysfs / 'fake_rail/ports/1/link_layer').write_text('InfiniBand')
        with self.assertRaisesRegex(RuntimeError, 'found 0'): E.choose_hca(profile, diag, sysfs)

    def test_rank_exec_uses_per_rank_gid_recipe_and_never_quick_or_ssh(self):
        plan = make_plan(self.root, 8, compare=True, functional={'bandwidth_iterations': 16})
        root = Path(plan['results_directory']);root.mkdir();write_before(plan, root)
        for rank in range(8):
            before = node_record(plan, rank)
            with mock.patch.dict(os.environ, {'SLURM_PROCID': str(rank), 'SLURM_LOCALID': '0', 'SLURM_JOB_ID': '123'}), \
                    mock.patch.object(E.socket, 'gethostname', return_value=f'node{rank}'), \
                    mock.patch.object(E, 'legacy_diag'), \
                    mock.patch.object(E, 'choose_hca', return_value={'global_v2_gid': before['global_v2_gid']}), \
                    mock.patch.object(E, 'bind_cpu', return_value={'affinity_after': [rank], 'assigned_cpu': rank}), \
                    mock.patch.object(E, 'tool_paths', return_value=before['tools']), \
                    mock.patch.object(os, 'execv') as execute, mock.patch('builtins.print'):
                E.rank_exec(plan, SCRIPTS, 'single-path')
                argv = execute.call_args.args[1]
                self.assertIn('--biters=16', argv)
                self.assertIn('--disable-retest', argv)
                self.assertNotIn('--quick', argv)
                self.assertNotIn('--bycore', argv)
                self.assertEqual(os.environ['UCX_IB_GID_INDEX'], str(3 + rank*2))
                self.assertEqual(os.environ['UCX_RC_MLX5_NUM_PATHS'], '1')
                self.assertEqual(os.environ['OMPI_MCA_hwloc_base_binding_policy'], 'none')
                self.assertEqual(read_json(root / f'rank-placement-single-path-{rank}.json')['gid_index'], 3 + rank*2)

    def test_preflight_never_executes_clusterkit_and_records_failure(self):
        plan = make_plan(self.root, 1);root = Path(plan['results_directory']);root.mkdir()
        with mock.patch.dict(os.environ, {'SLURM_PROCID': '0', 'SLURM_LOCALID': '0', 'SLURM_JOB_ID': '123'}), \
                mock.patch.object(E.socket, 'gethostname', return_value='node0'), \
                mock.patch.object(E.platform, 'machine', return_value='wrong'), \
                mock.patch.object(os, 'execv', side_effect=AssertionError('no benchmark')):
            with self.assertRaisesRegex(RuntimeError, 'architecture'): E.preflight_node(plan, SCRIPTS)
            self.assertEqual(read_json(root / 'node-0-before.json')['status'], 'FAIL')
            with self.assertRaisesRegex(RuntimeError, 'No ClusterKit invocation'): E.rank_exec(plan, SCRIPTS, 'baseline')

    def test_batch_simulates_1_2_4_8_without_real_scheduler_or_benchmarks(self):
        for count in (1, 2, 4, 8):
            plan = make_plan(self.root, count, compare=count != 1)
            root = Path(plan['results_directory']);root.mkdir()
            calls = []
            def simulated_step(argv, logfile, seconds, _calls=calls, _plan=plan, _root=root):
                _calls.append((argv, seconds))
                phase = argv[-1]
                if phase == 'before': write_before(_plan, _root)
                elif phase in ('baseline', 'single-path'): write_stage(_plan, _root, phase)
                return 0
            with mock.patch.object(E, 'allocation', return_value='123'), \
                    mock.patch.object(E, 'checked_output', return_value='fixture pmix_v3'), \
                    mock.patch.object(E, 'run_step', side_effect=simulated_step), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('real process')):
                self.assertEqual(E.batch(plan, root/'plan.json', SCRIPTS, 'preflight' if count==1 else 'pairwise'), 0)
            result = read_json(root/'summary.json')
            self.assertEqual(result['functional_status'], 'PASS', result['errors'])
            self.assertEqual(result['performance_status'], 'UNVERIFIED')
            for argv, seconds in calls:
                self.assertEqual(argv[0], 'srun')
                self.assertIn('--nodes=' + str(count), argv)
                self.assertIn('--ntasks=' + str(count), argv)
                self.assertIn('--gres=none', argv)
                self.assertNotIn('--exclusive', argv)
                self.assertGreater(seconds, 0)
                self.assertLessEqual(seconds, 90)
            self.assertEqual([v[0][-1] for v in calls], ['before', 'after'] if count==1 else ['before', 'baseline', 'single-path', 'after'])
            with self.assertRaises(FileExistsError):
                E.batch(plan, root/'plan.json', SCRIPTS, 'preflight' if count==1 else 'pairwise')

    def test_baseline_failure_is_not_hidden_by_diagnostics_and_no_retry(self):
        plan = make_plan(self.root, 4, compare=True);root = Path(plan['results_directory']);root.mkdir()
        phases = []
        def simulated_step(argv, logfile, seconds):
            phase = argv[-1];phases.append(phase)
            if phase == 'before': write_before(plan, root)
            return 124 if phase == 'baseline' else 0
        with mock.patch.object(E, 'allocation', return_value='123'), \
                mock.patch.object(E, 'checked_output', return_value='pmix_v3'), \
                mock.patch.object(E, 'run_step', side_effect=simulated_step), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(E.batch(plan, root/'plan.json', SCRIPTS, 'pairwise'), 124)
        self.assertEqual(phases, ['before', 'baseline', 'after'])
        summary = read_json(root/'summary.json')
        self.assertEqual(summary['primary_exit_status'], 124)
        self.assertEqual(summary['functional_status'], 'FAIL')

    def test_preflight_failure_prevents_all_benchmark_steps(self):
        plan = make_plan(self.root, 4);root = Path(plan['results_directory']);root.mkdir()
        with mock.patch.object(E, 'allocation', return_value='123'), \
                mock.patch.object(E, 'checked_output', return_value='pmix_v3'), \
                mock.patch.object(E, 'run_step', return_value=9) as step, mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(E.batch(plan, root/'plan.json', SCRIPTS, 'pairwise'), 9)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(read_json(root/'summary.json')['functional_status'], 'FAIL')

    def test_missing_reviewed_pmix_plugin_blocks_preflight_without_launching_tasks(self):
        plan = make_plan(self.root, 1);root = Path(plan['results_directory']);root.mkdir()
        with mock.patch.object(E, 'allocation', return_value='123'), \
                mock.patch.object(E, 'checked_output', return_value='pmi2 only'), \
                mock.patch.object(E, 'run_step', side_effect=AssertionError('no task launch')) as step:
            self.assertEqual(E.batch(plan, root/'plan.json', SCRIPTS, 'preflight'), 1)
        step.assert_not_called()
        self.assertIn('PMIx', ' '.join(read_json(root/'summary.json')['errors']))

    def test_timeout_terminates_and_reaps_only_its_own_process_group(self):
        process = mock.Mock(pid=999999)
        process.wait.side_effect = [subprocess.TimeoutExpired('fake-step', 1), 0]
        with mock.patch.object(subprocess, 'Popen', return_value=process) as popen, \
                mock.patch.object(os, 'killpg') as kill:
            self.assertEqual(E.run_step(['fake-step'], self.root/'step.log', 1), 124)
            kill.assert_called_once_with(999999, signal.SIGTERM)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)


    def test_successful_preflight_checks_tools_and_records_rank_seven(self):
        plan = make_plan(self.root, 8);root = Path(plan['results_directory']);root.mkdir()
        before = node_record(plan, 7)
        network = {'host': 'node7', 'hca': before['hca'], 'global_v2_gid': before['global_v2_gid'],
                   'link_layer': 'Ethernet'}
        with mock.patch.dict(os.environ, {'SLURM_PROCID': '7', 'SLURM_LOCALID': '0', 'SLURM_JOB_ID': '123'}), \
                mock.patch.object(E.socket, 'gethostname', return_value='node7'), \
                mock.patch.object(E.platform, 'machine', return_value='aarch64'), \
                mock.patch.object(E, 'legacy_diag'), \
                mock.patch.object(E, 'choose_hca', return_value=network), \
                mock.patch.object(E, 'bind_cpu', return_value={'assigned_cpu': 7, 'affinity_after': [7]}), \
                mock.patch.object(E, 'tool_paths', return_value=before['tools']), \
                mock.patch.object(E, 'checked_output', return_value='fixture Device: fake_rail:1\n') as read, \
                mock.patch.object(os, 'execv', side_effect=AssertionError('no benchmark')):
            E.preflight_node(plan, SCRIPTS)
            record = read_json(root/'node-7-before.json')
            self.assertEqual(record['status'], 'PASS')
            self.assertEqual(record['gid_index'], 17)
            self.assertEqual(read.call_count, 3)

    def test_gid_change_after_preflight_prevents_clusterkit_start(self):
        plan = make_plan(self.root);root = Path(plan['results_directory']);root.mkdir();write_before(plan, root)
        with mock.patch.dict(os.environ, {'SLURM_PROCID': '0', 'SLURM_LOCALID': '0', 'SLURM_JOB_ID': '123'}), \
                mock.patch.object(E.socket, 'gethostname', return_value='node0'), \
                mock.patch.object(E, 'legacy_diag'), \
                mock.patch.object(E, 'choose_hca', return_value={'global_v2_gid': {'index': 99}}), \
                mock.patch.object(os, 'execv', side_effect=AssertionError('no benchmark')), \
                self.assertRaisesRegex(RuntimeError, 'GID changed'):
            E.rank_exec(plan, SCRIPTS, 'baseline')

    def test_duplicate_global_gid_blocks_benchmark_even_when_preflight_step_exits_zero(self):
        plan = make_plan(self.root, 4);root = Path(plan['results_directory']);root.mkdir()
        def simulated_step(argv, logfile, seconds):
            self.assertEqual(argv[-1], 'before')
            for rank in range(4):
                record = node_record(plan, rank);record['global_v2_gid']['gid'] = '2001:db8::1'
                write_json(root / f'node-{rank}-before.json', record)
            return 0
        with mock.patch.object(E, 'allocation', return_value='123'), \
                mock.patch.object(E, 'checked_output', return_value='pmix_v3'), \
                mock.patch.object(E, 'run_step', side_effect=simulated_step) as step, mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(E.batch(plan, root/'plan.json', SCRIPTS, 'pairwise'), 1)
        self.assertEqual(step.call_count, 1)
        self.assertIn('duplicate', ' '.join(read_json(root/'summary.json')['errors']))

    def test_sigterm_during_a_step_is_recorded_as_failure_without_retry(self):
        plan = make_plan(self.root);root = Path(plan['results_directory']);root.mkdir()
        def interrupted_step(*args):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        previous = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(E, 'allocation', return_value='123'), \
                mock.patch.object(E, 'checked_output', return_value='pmix_v3'), \
                mock.patch.object(E, 'run_step', side_effect=interrupted_step) as step, mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(E.batch(plan, root/'plan.json', SCRIPTS, 'pairwise'), 1)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertIn('signal 15', ' '.join(read_json(root/'summary.json')['errors']))

    def test_resource_recipe_limits_and_insufficient_wall_time_fail_closed(self):
        base = make_plan(self.root)['effective_profile']
        for params in [{'bandwidth_bytes': 2**30}, {'latency_bytes': 2**25}, {'bandwidth_iterations': 10**9}]:
            raw = dict(base, functional=params)
            self.assertFalse(parse_profile(raw).ready)
        plan = make_plan(self.root, default_wall_seconds=60)
        self.assertFalse(plan['entrypoint_supported'])
        with self.assertRaisesRegex(ValueError, 'unsupported'): E.prepare_attempt(plan, SCRIPTS)

    def test_static_rank_shell_preserves_literal_arguments_and_loads_fake_hpcx_only(self):
        # Exercise the actual reviewed shell with an isolated fake CLI, never the compute CLI.
        import shutil
        scripts = self.root / 'scripts with spaces';scripts.mkdir()
        shutil.copyfile(SCRIPTS/'clusterkit_rank.sh', scripts/'clusterkit_rank.sh')
        init = self.root / "fake hpcx's init.sh"
        init.write_text('hpcx_load() { export FAKE_HPCX_LOADED=yes; }\n')
        fake = scripts/'clusterkit_execute.py'
        fake.write_text("import sys,os,json\nfrom pathlib import Path\n"
                        "if sys.argv[1]=='init-path': print(Path(__file__).parent.parent.joinpath(\"fake hpcx's init.sh\"))\n"
                        "else: print(json.dumps({'args':sys.argv[1:], 'loaded':os.environ.get('FAKE_HPCX_LOADED')}))\n")
        planpath = self.root/'literal $(touch BAD).json'
        result = subprocess.run(['bash', str(scripts/'clusterkit_rank.sh'), str(planpath), 'before', str(scripts)],
                                capture_output=True, text=True, timeout=10, cwd=self.root, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['loaded'], 'yes')
        self.assertEqual(output['args'], ['node', '--plan', str(planpath), '--stage', 'before'])
        self.assertFalse((self.root/'BAD').exists())


class ResultsTests(unittest.TestCase):
    def test_unique_coverage_for_all_supported_sizes_with_reversed_vendor_ranks(self):
        for count in (2, 4, 8):
            with tempfile.TemporaryDirectory() as directory:
                plan = make_plan(Path(directory), count)
                for test in ('bandwidth', 'latency'):
                    result = decode_pairs(payload(plan, test), plan['nodes'], test, '123')
                    self.assertEqual(result['observed_pair_count'], count*(count-1)//2)
                    self.assertEqual(result['missing_pairs'], [])
                    self.assertEqual(result['failed_pairs'], [])

    def test_missing_zero_negative_and_nonfinite_pairs_are_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = make_plan(Path(directory), 4)
            raw = payload(plan, 'bandwidth');raw['Links'][0] = [0, 0, -1]
            result = decode_pairs(raw, plan['nodes'], 'bandwidth', '123')
            self.assertEqual(len(result['failed_pairs']), 2)
            self.assertEqual(len(result['missing_pairs']), 1)
            for value in (float('nan'), float('inf'), 'bad', True):
                bad = payload(plan, 'bandwidth');bad['Links'][0][1] = value
                with self.assertRaises(ValueError): decode_pairs(bad, plan['nodes'], 'bandwidth', '123')

    def test_foreign_nodes_jobs_units_and_malformed_rank_map_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = make_plan(Path(directory), 2)
            for key, value in [('Nodes', {'node0': 0, 'outside': 1}), ('Nodes', {'node0': 0, 'node1': 0}),
                               ('JOBID', 999), ('Units', 'GB/s'), ('Links', [[0, 1, 2]])]:
                raw = payload(plan, 'bandwidth');raw[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    decode_pairs(raw, plan['nodes'], 'bandwidth', '123')

    def test_exit_zero_with_missing_coverage_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = make_plan(Path(directory), 4);root = Path(plan['results_directory']);root.mkdir()
            write_before(plan, root);write_stage(plan, root, 'baseline')
            path = root/'baseline/vendor-date/bandwidth.json'
            raw = json.loads(path.read_text());raw['Links'][0] = [0, 10];path.write_text(json.dumps(raw))
            outcome = {'job_id': '123', 'preflight_exit': 0, 'primary_exit': 0, 'stages': {'baseline': 0}}
            result = summarize(plan, root, outcome)
            self.assertEqual(result['functional_status'], 'FAIL')
            self.assertEqual(result['performance_status'], 'UNVERIFIED')
            self.assertEqual(len(result['stages']['baseline']['metrics']['bandwidth']['missing_pairs']), 2)

    def test_failed_post_diagnostics_warns_without_claiming_performance_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = make_plan(Path(directory), 2);root = Path(plan['results_directory']);root.mkdir()
            write_before(plan, root);write_stage(plan, root, 'baseline')
            outcome = {'job_id': '123', 'preflight_exit': 0, 'primary_exit': 0,
                       'stages': {'baseline': 0}, 'diagnostics': {'after_exit': 7}}
            result = summarize(plan, root, outcome)
            self.assertEqual(result['preflight']['status'], 'PASS')
            self.assertEqual(result['functional_status'], 'WARN')
            self.assertEqual(result['performance_status'], 'UNVERIFIED')

    def test_malformed_placement_fails_summary_without_losing_other_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = make_plan(Path(directory), 2);root = Path(plan['results_directory']);root.mkdir()
            write_before(plan, root);write_stage(plan, root, 'baseline')
            (root/'rank-placement-baseline-1.json').write_text('[]')
            outcome = {'job_id': '123', 'preflight_exit': 0, 'primary_exit': 0, 'stages': {'baseline': 0}}
            result = summarize(plan, root, outcome)
            self.assertEqual(result['functional_status'], 'FAIL')
            self.assertEqual(result['preflight']['status'], 'PASS')
            self.assertEqual(result['stages']['baseline']['metrics']['bandwidth']['observed_pair_count'], 1)

    def test_legacy_cell_behavior_remains_identical(self):
        # Shared function was extracted without changing the legacy API behavior.
        from clusterkit_metrics import get_cell
        self.assertEqual(get_cell([[0, 5, 7], [0, 9]], 2, 0), 7)
        self.assertEqual(get_cell([], 1, 1), 0)
        self.assertIsNone(get_cell([[0]], 0, 1))


if __name__ == '__main__':
    unittest.main()
