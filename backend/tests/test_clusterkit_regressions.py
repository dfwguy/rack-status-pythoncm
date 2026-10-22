# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Offline regressions: disposable files/fake schedulers, external execution denied."""
import ast
import copy
import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if __package__:
    from . import test_clusterkit_service as fixtures
else:
    import test_clusterkit_service as fixtures
import clusterkit_artifacts as artifacts
import clusterkit_context as context
import clusterkit_service as service
from clusterkit_attempts import SubmissionBusyError, load_intent
from clusterkit_summary import validate_summary
from request_policy import is_print_only_request
from schedulers import JobHandle
from schedulers.slurm import SlurmScheduler


class RegressionTests(unittest.TestCase):
    resolution = fixtures.ClusterKitServiceTests.resolution
    request = fixtures.ClusterKitServiceTests.request

    def setUp(self):
        fixtures.ClusterKitServiceTests.setUp(self)
        deny = mock.patch('subprocess.Popen', side_effect=AssertionError('No external process allowed in regressions'))
        deny.start()
        self.addCleanup(deny.stop)

    def preview(self, count=2, resolution=None):
        resolution = resolution or self.resolution()
        reader = fixtures.FakeReader(self.nodes)
        reply = service.preview(self.request(count), resolution, reader,
            scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1000)
        return reply, resolution, reader

    def submit(self, count=2, scheduler=None, registry=None):
        reply, resolution, reader = self.preview(count)
        scheduler = scheduler or fixtures.FakeScheduler()
        registry = registry or fixtures.FakeRegistry()
        result = service.submit_confirmed(reply['confirmation_token'],resolution,reader,scheduler,registry,
            scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)
        return result, scheduler, registry, reply, resolution, reader

    def test_parent_and_final_symlinks_are_rejected(self):
        root = self.results/'attempt'; root.mkdir()
        outside = self.root/'outside'; outside.mkdir(); (outside/'log.txt').write_text('outside')
        (root/'parent').symlink_to(outside,target_is_directory=True)
        (root/'final.txt').symlink_to(outside/'log.txt')
        for name in ['parent/log.txt','final.txt']:
            with self.subTest(name=name), self.assertRaises((OSError,ValueError)):
                service.artifact({'log_dir':str(root)},name)
        self.assertEqual(service.artifacts({'log_dir':str(root)}),[])

    def test_symlink_in_root_ancestors_is_rejected(self):
        target=self.results/'real'; target.mkdir(); (target/'log.txt').write_text('inside')
        link=self.root/'alias'; link.symlink_to(self.results,target_is_directory=True)
        with self.assertRaises((OSError,ValueError)):
            service.artifact({'log_dir':str(link/'real')},'log.txt')

    def test_artifact_traversal_and_noncanonical_names_rejected(self):
        for name in ['../x','/etc/passwd','a//b','a/./b','a/../b','a/','a/b/c/d/e','']:
            with self.subTest(name=name),self.assertRaises(ValueError):
                service.artifact({'log_dir':str(self.results)},name)

    def test_oversized_file_rejected_before_read(self):
        p=self.results/'large.log'; p.write_bytes(b'123456789')
        class NoRead:
            def __init__(self,fd): self.fd=fd
            def __enter__(self): return self
            def __exit__(self,*args): os.close(self.fd)
            def fileno(self): return self.fd
            def read(self,*args): raise AssertionError('Oversized artifact must not be loaded')
        with mock.patch.object(service,'MAX_ARTIFACT_BYTES',8), mock.patch.object(artifacts.os,'fdopen',side_effect=lambda fd,*a:NoRead(fd)), self.assertRaisesRegex(ValueError,'limit'):
            service.artifact({'log_dir':str(self.results)},'large.log')

    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.results/'fifo')
        with self.assertRaises(ValueError): service.artifact({'log_dir':str(self.results)},'fifo')

    def test_parent_replacement_cannot_redirect_open_descriptor(self):
        parent=self.results/'parent'; parent.mkdir(); (parent/'log.txt').write_text('original')
        outside=self.root/'outside'; outside.mkdir(); (outside/'log.txt').write_text('outside')
        real_open=os.open
        def replaced(name,flags,*args,**kwargs):
            if name=='log.txt':
                parent.rename(self.results/'held')
                parent.symlink_to(outside,target_is_directory=True)
            return real_open(name,flags,*args,**kwargs)
        with mock.patch.object(artifacts.os,'open',side_effect=replaced):
            data,_=service.artifact({'log_dir':str(self.results)},'parent/log.txt')
        self.assertEqual(data,b'original')

    def test_tracking_failure_preserves_job_and_receipt_without_cancel(self):
        class Broken(fixtures.FakeRegistry):
            def add_or_update_task(self,*args,**kwargs): raise RuntimeError('tracking unavailable')
        result,scheduler,_registry,*_=self.submit(registry=Broken())
        self.assertEqual(result['scheduler_job_id'],'12345')
        self.assertEqual(scheduler.cancelled,[])
        self.assertTrue(result['warnings'])
        receipt=json.loads((Path(result['results_directory'])/'submission-receipt.json').read_text())
        self.assertEqual(receipt['job_id'],'12345')

    def test_receipt_precedes_mutable_registry_tracking(self):
        observed=[]
        class Checking(fixtures.FakeRegistry):
            def add_or_update_task(inner,activity_id,**fields):
                root=Path(inner.rows[activity_id]['log_dir'])
                observed.append((root/'submission-receipt.json').is_file())
                return super().add_or_update_task(activity_id,**fields)
        self.submit(registry=Checking())
        self.assertEqual(observed,[True])

    def test_lost_submission_reply_recovers_without_retry_or_cancel(self):
        class Lost(fixtures.FakeScheduler):
            def submit(inner,spec):
                inner.specs.append(spec); inner.recovered_handle=JobHandle('12345','slurm')
                raise TimeoutError('accepted but reply lost')
        result,scheduler,registry,*_=self.submit(scheduler=Lost())
        self.assertEqual(result['submission_state'],'SUBMISSION_UNKNOWN')
        reconciled=service.reconcile(result['activity_id'],registry,scheduler)
        self.assertEqual(reconciled['activity']['tasks'][0]['scheduler_ref'],'12345')
        self.assertEqual(len(scheduler.specs),1)
        self.assertEqual(scheduler.cancelled,[])
        self.assertTrue((Path(result['results_directory'])/'submission-receipt.json').is_file())

    def test_unknown_acceptance_blocks_new_overlapping_attempt(self):
        class Lost(fixtures.FakeScheduler):
            def submit(inner,spec): inner.specs.append(spec); raise TimeoutError('lost')
        result,scheduler,registry,_,resolution,reader=self.submit(scheduler=Lost())
        for _ in range(2):
            status=service.reconcile(result['activity_id'],registry,scheduler)
            self.assertEqual(status['scheduler_state'],'SUBMISSION_UNKNOWN')
        fresh=service.preview(self.request(),resolution,reader,scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)
        with self.assertRaisesRegex(ValueError,'SUBMISSION_UNKNOWN'):
            service.submit_confirmed(fresh['confirmation_token'],resolution,reader,scheduler,registry,
                scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1002)
        self.assertEqual(len(scheduler.specs),1)
        self.assertEqual(scheduler.cancelled,[])

    def test_registry_failure_before_submission_never_contacts_scheduler(self):
        class Broken(fixtures.FakeRegistry):
            def record_activity(self,*args,**kwargs): raise RuntimeError('registry unavailable')
        reply,resolution,reader=self.preview(); scheduler=fixtures.FakeScheduler()
        with self.assertRaises(RuntimeError):
            service.submit_confirmed(reply['confirmation_token'],resolution,reader,scheduler,Broken(),scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)
        self.assertEqual(scheduler.specs,[])

    def test_parallel_duplicate_confirmation_submits_once(self):
        reply,resolution,reader=self.preview(); scheduler=fixtures.FakeScheduler(); registry=fixtures.FakeRegistry()
        def attempt(_):
            try:
                return service.submit_confirmed(reply['confirmation_token'],resolution,reader,scheduler,registry,scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)['scheduler_job_id']
            except (service.DuplicateSubmissionError, SubmissionBusyError): return 'duplicate'
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(attempt,range(2)))
        self.assertCountEqual(results,['12345','duplicate'])
        self.assertEqual(len(scheduler.specs),1)

    def test_registry_reload_recovers_durable_intent_and_receipt(self):
        result,scheduler,_,_,resolution,_=self.submit()
        registry=fixtures.FakeRegistry()
        ctx=json.loads((Path(result['results_directory'])/'submission-intent.json').read_text())['context']
        service.recover_activity(result['activity_id'],resolution,registry,context=ctx)
        status=service.reconcile(result['activity_id'],registry,scheduler,context=ctx)
        self.assertEqual(status['activity']['tasks'][0]['scheduler_ref'],'12345')
        self.assertEqual(len(scheduler.specs),1)

    def test_changed_context_is_rejected_before_scheduler_query(self):
        result,scheduler,registry,*_=self.submit()
        intent,_=load_intent(result['results_directory'])
        changed=copy.deepcopy(intent['context']); changed['selection']['cluster']='other'
        with self.assertRaisesRegex(ValueError,'context changed'):
            service.reconcile(result['activity_id'],registry,scheduler,context=changed)
        self.assertFalse(hasattr(scheduler,'last_status'))

    def test_remote_execution_rejected_before_discovery_or_staging(self):
        remote=SimpleNamespace(kind='ssh')
        with self.assertRaisesRegex(ValueError,'local execution only'):
            context.capture(str(fixtures.SCRIPTS),remote)
        self.assertEqual(list(self.results.iterdir()),[])

    def test_changed_recipe_is_rejected_and_timestamp_changes_are_allowed(self):
        reply,resolution,reader=self.preview()
        confirmed=service.confirm_plan(reply['confirmation_token'],resolution,reader,scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)
        self.assertNotEqual(confirmed['inventory']['captured_at'],reply['plan']['inventory']['captured_at'])
        with mock.patch.object(service,'recipe_digest',return_value='changed'),self.assertRaisesRegex(service.ConfirmationError,'recipe'):
            service.confirm_plan(reply['confirmation_token'],resolution,reader,scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)

    def test_changed_script_directory_rejected(self):
        reply,resolution,reader=self.preview()
        with self.assertRaises(service.ConfirmationError):
            service.confirm_plan(reply['confirmation_token'],resolution,reader,scripts_dir=str(fixtures.SCRIPTS)+'-other',secret=self.secret,now=1001)

    def test_preview_and_provider_argv_match_for_all_sizes(self):
        for count in [1,2,4,8]:
            request={'scope':'preflight' if count==1 else 'explicit','nodes':self.nodes[:count]}
            reply=service.preview(request,self.resolution(),fixtures.FakeReader(self.nodes),scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1000)
            plan=reply['plan']
            self.assertEqual(plan['sbatch_argv'],['sbatch',*SlurmScheduler.render_argv(service.job_spec(plan))])
            self.assertIn('--comment=rack-clusterkit:'+plan['attempt_id'],plan['sbatch_argv'])
            self.assertEqual(plan['expected_pair_count'],count*(count-1)//2)
        self.assertEqual(list(self.results.iterdir()),[])

    def test_wrong_or_minimal_summary_never_passes(self):
        result,scheduler,registry,*_=self.submit(); scheduler.state='COMPLETED'
        root=Path(result['results_directory'])
        (root/'summary.json').write_text(json.dumps({'functional_status':'PASS','attempt_id':'wrong'}))
        status=service.reconcile(result['activity_id'],registry,scheduler)
        self.assertEqual(status['activity']['work_phase'],'failed')
        self.assertTrue(status['summary_error'])

    def test_summary_binding_missing_pairs_and_exit_failures(self):
        result,_,_,_reply,*_=self.submit(count=4)
        root=Path(result['results_directory']); plan=json.loads((root/'plan.json').read_text())
        valid=fixtures.write_passing_summary(plan,root)
        self.assertEqual(validate_summary(valid,plan,'12345')['functional_status'],'PASS')
        mutations=[lambda x:x.update(attempt_id='wrong'),lambda x:x.update(slurm_job_id='999'),
                   lambda x:x.update(nodes=list(reversed(x['nodes']))),lambda x:x.update(plan_hash='wrong'),
                   lambda x:x.update(primary_exit_status=1),lambda x:x.update(performance_status='PASS'),
                   lambda x:x['stages']['baseline']['metrics']['latency']['pairs'].pop(),
                   lambda x:x['preflight']['nodes'][1].update(gid_index=-1),
                   lambda x:x['stages']['baseline']['placements'][1].update(rank=0)]
        for mutate in mutations:
            candidate=copy.deepcopy(valid); mutate(candidate)
            with self.subTest(mutation=mutate),self.assertRaises((ValueError,TypeError)):
                validate_summary(candidate,plan,'12345')

    def test_completed_terminal_state_is_not_regressed_to_unknown(self):
        result,scheduler,registry,*_=self.submit()
        root=Path(result['results_directory']); fixtures.write_passing_summary(json.loads((root/'plan.json').read_text()),root)
        scheduler.state='COMPLETED'; service.reconcile(result['activity_id'],registry,scheduler)
        scheduler.state='UNKNOWN'; status=service.reconcile(result['activity_id'],registry,scheduler)
        self.assertEqual(status['scheduler_state'],'COMPLETED')
        self.assertEqual(status['activity']['work_phase'],'done')

    def test_scheduler_missing_records_are_unknown_and_errors_are_not_pending(self):
        class FakeConn:
            def __init__(self,code): self.code=code; self.calls=[]
            def run(self,command,**kwargs): self.calls.append(command); return subprocess.CompletedProcess(command,self.code,'','')
        conn=FakeConn(0); scheduler=SlurmScheduler(conn)
        self.assertEqual(scheduler.status(JobHandle('12345','slurm')),'UNKNOWN')
        self.assertEqual(len(conn.calls),2)
        with self.assertRaises(RuntimeError): SlurmScheduler(FakeConn(1)).status(JobHandle('12345','slurm'))

    def test_scheduler_queue_fallback_and_recovery_tag_disambiguation(self):
        name='ck-'+'a'*32; tag='rack-clusterkit:'+'a'*32
        class FakeConn:
            def run(self,command,**kwargs):
                if '--name=' in command:
                    out=f'12345|{name}|{tag}\n' if 'squeue' in command else ''
                    return subprocess.CompletedProcess(command,0,out,'')
                return subprocess.CompletedProcess(command,0,'RUNNING\n','') if 'squeue' in command else subprocess.CompletedProcess(command,1,'','error')
        scheduler=SlurmScheduler(FakeConn())
        self.assertEqual(scheduler.status(JobHandle('12345','slurm')),'RUNNING')
        self.assertEqual(scheduler.find_by_tag(name,tag,'2026-10-21T00:00:00+00:00','cmods').job_id,'12345')

    def test_read_only_request_policy_does_not_classify_submit_or_status(self):
        self.assertTrue(is_print_only_request('/api/clusterkit/preview','POST'))
        self.assertTrue(is_print_only_request('/api/clusterkit/inventory','GET'))
        self.assertFalse(is_print_only_request('/api/clusterkit/runs','POST'))
        self.assertFalse(is_print_only_request('/api/clusterkit/runs/123','GET'))

    def test_existing_global_hook_bodies_honor_read_only_policy_and_auth(self):
        tree=ast.parse((fixtures.REPO/'backend/app.py').read_text())
        body=[]
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(n,ast.Name) and n.id in {'_LAKE_CAPTURE_SKIP','_LAKE_CAPTURE_GET_ALLOW'} for n in node.targets): body.append(node)
            if isinstance(node,ast.FunctionDef) and node.name in {'_require_dashboard_auth','_lake_should_capture'}:
                node.decorator_list=[]; body.append(node)
        bootstrap=mock.Mock()
        space={'is_print_only_request':is_print_only_request,'_lake_capture_mode':lambda:'all',
               '_JOB_ENV_BOOTSTRAP_DONE':False,'_job_env_overrides_load':bootstrap,
               'request':SimpleNamespace(path='/api/clusterkit/preview',method='POST',endpoint='clusterkit_modular.clusterkit_preview'),
               '_auth_enabled':lambda:True,'_is_public_spa_static_path':lambda:False,'_session_ok':lambda:False,
               'jsonify':lambda value:value,'_logger':mock.Mock()}
        exec(compile(ast.Module(body=body,type_ignores=[]),'exact_global_hook_bodies','exec'),space)  # noqa: S102 - Exact local hook bodies, no external input.
        self.assertFalse(space['_lake_should_capture']('/api/clusterkit/preview','POST'))
        result=space['_require_dashboard_auth']()
        self.assertEqual(result[1],401)
        bootstrap.assert_not_called()
        self.assertFalse(space['_JOB_ENV_BOOTSTRAP_DONE'])

    def test_real_durable_registry_reloads_in_a_fresh_module_instance(self):
        import importlib.util
        def registry_instance(name):
            spec=importlib.util.spec_from_file_location(name,fixtures.BACKEND/'validation_activities.py')
            registry=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(registry)
            for key in ['ACTIVITIES_FILE','LEGACY_JOBS_FILE','ACTIVITY_ARCHIVE_DB',
                        'MULTIRACK_LEDGER_FILE','MULTIRACK_LEDGER_MARKDOWN_FILE',
                        'MULTIRACK_LEDGER_EXPORT_STATUS_FILE']:
                patch=mock.patch.object(registry,key,self.root/('store-'+key))
                patch.start(); self.addCleanup(patch.stop)
            patch=mock.patch.object(registry,'_ledger_lake_disabled',return_value=True)
            patch.start(); self.addCleanup(patch.stop)
            return registry
        registry=registry_instance('offline_clusterkit_store_before')
        result,scheduler,_,*_=self.submit(registry=registry)
        self.assertTrue(registry.ACTIVITY_ARCHIVE_DB.is_file())
        fresh=registry_instance('offline_clusterkit_store_after')
        self.assertEqual(fresh.get_activity(result['activity_id'])['tasks'][0]['scheduler_ref'],'12345')
        scheduler.state='COMPLETED'
        root=Path(result['results_directory'])
        fixtures.write_passing_summary(json.loads((root/'plan.json').read_text()),root)
        status=service.reconcile(result['activity_id'],fresh,scheduler)
        self.assertEqual(status['activity']['work_phase'],'done')

    def test_receipt_write_failure_retains_known_job_and_can_recover_missing_receipt(self):
        original=service.write_record
        def failed(root,name,value):
            if name=='submission-receipt.json': raise OSError('fixture receipt write failure')
            return original(root,name,value)
        with mock.patch.object(service,'write_record',side_effect=failed):
            result,scheduler,registry,*_=self.submit()
        self.assertEqual(result['scheduler_job_id'],'12345')
        self.assertEqual(scheduler.cancelled,[])
        scheduler.recovered_handle=JobHandle('12345','slurm')
        status=service.reconcile(result['activity_id'],registry,scheduler)
        self.assertEqual(status['activity']['tasks'][0]['scheduler_ref'],'12345')
        self.assertTrue((Path(result['results_directory'])/'submission-receipt.json').is_file())

    def test_silent_registry_failure_prevents_submission(self):
        class Silent(fixtures.FakeRegistry):
            def record_activity(self,*args,**kwargs): return 'not-written'
        reply,resolution,reader=self.preview(); scheduler=fixtures.FakeScheduler()
        with self.assertRaisesRegex(RuntimeError,'activity write'):
            service.submit_confirmed(reply['confirmation_token'],resolution,reader,scheduler,Silent(),
                scripts_dir=str(fixtures.SCRIPTS),secret=self.secret,now=1001)
        self.assertEqual(scheduler.specs,[])

    def test_pinned_connection_removes_inherited_sbatch_overrides(self):
        ctx=context.capture(str(fixtures.SCRIPTS))
        connection=context.PinnedLocalConnection(ctx)
        with mock.patch.dict(os.environ,{'SBATCH_EXCLUSIVE':'1','SLURM_CLUSTERS':'changed'}), mock.patch.object(connection.executor,'run',return_value=None) as run:
            connection.run('fixture-command')
        environment=run.call_args.kwargs['env']
        self.assertNotIn('SBATCH_EXCLUSIVE',environment)
        self.assertEqual(environment.get('SLURM_CLUSTERS',''),ctx['slurm_environment']['SLURM_CLUSTERS'])

    def test_malformed_collection_timestamp_cannot_extend_grace_forever(self):
        self.assertGreater(service._age_seconds('broken'),1800)

    def test_shared_activity_hydration_dispatches_before_generic_slurm_queries(self):
        tree=ast.parse((fixtures.REPO/'backend/app.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_hydrate_activity_task_logs')
        forbidden=mock.Mock(side_effect=AssertionError('Generic activity ladder must not run for modular ClusterKit'))
        space={'validation_activities':SimpleNamespace(get_activity=forbidden),
               '_task_needs_slurm_hydration':forbidden,'native_status':SimpleNamespace(load_squeue_jobs_map=forbidden)}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'exact_activity_hydrator','exec'),space)  # noqa: S102 - Exact local hook body, no external input.
        row={'source':'clusterkit-api','activity_id':'fixture'}
        with mock.patch.object(service,'refresh_activity_record',return_value={'activity':{'phase':'running'}}) as refresh:
            space['_hydrate_activity_task_logs']('fixture',row)
        refresh.assert_called_once()
        forbidden.assert_not_called()
        self.assertEqual(row['phase'],'running')

    def test_transport_snapshot_uses_read_only_settings_without_initializing_cached_connection(self):
        import config_env
        import host_exec
        values={'RACK_EXEC_MODE':'local','RACK_REMOTE_BASE_PATH':'/reviewed/tree','RACK_STATUS_LOG_DIR':None}
        with mock.patch.object(config_env,'read_only_values',return_value=values), mock.patch.object(config_env,'_CATALOG_AVAILABLE',False), mock.patch.object(host_exec,'_ensure_init',side_effect=AssertionError('Cached/writable bootstrap forbidden')):
            roles, roots=host_exec.read_only_roles()
        self.assertEqual(roles.select('control').kind,'local')
        self.assertEqual(roots['scripts'],'/reviewed/tree/bin/scripts')

    def test_full_submission_command_includes_the_pinned_platform_wrapper(self):
        reply,_,_=self.preview()
        class FakeConn:
            def run(inner,command,**kwargs):
                inner.command=command
                return subprocess.CompletedProcess(command,0,'12345\n','')
        conn=FakeConn()
        scheduler=SlurmScheduler(conn,context.PinnedPlatform(reply['execution_context']))
        scheduler.submit(service.job_spec(reply['plan']))
        self.assertEqual(conn.command,reply['submission_command_display'])

    def test_preview_provides_recovery_identity_before_submission_response(self):
        result,_,_,reply,*_=self.submit()
        self.assertEqual(reply['activity_id'],result['activity_id'])
        self.assertEqual(reply['status_url'],result['status_url'])

    def test_existing_job_poller_contract_separates_activity_and_scheduler_ids(self):
        result,scheduler,registry,*_=self.submit()
        self.assertEqual(result['job_id'],result['activity_id'])
        self.assertNotEqual(result['job_id'],result['scheduler_job_id'])
        self.assertEqual(result['phase'],'running')
        status=service.reconcile(result['job_id'],registry,scheduler)
        self.assertEqual(status['job_id'],result['activity_id'])
        self.assertEqual(status['scheduler_job_id'],'12345')
        self.assertEqual(status['phase'],'running')
        self.assertEqual(status['tasks'][0]['scheduler_ref'],'12345')

    def test_existing_job_poller_sees_done_only_after_validated_summary(self):
        result,scheduler,registry,*_=self.submit()
        scheduler.state='COMPLETED'
        pending=service.reconcile(result['job_id'],registry,scheduler)
        self.assertEqual(pending['phase'],'running')
        self.assertEqual(pending['work_phase'],'collecting')
        root=Path(result['results_directory'])
        fixtures.write_passing_summary(json.loads((root/'plan.json').read_text()),root)
        complete=service.reconcile(result['job_id'],registry,scheduler)
        self.assertEqual(complete['phase'],'done')
        self.assertEqual(complete['returncode'],0)
        self.assertEqual(complete['summary']['functional_status'],'PASS')
        self.assertEqual(complete['summary']['performance_status'],'UNVERIFIED')

    def test_existing_job_poller_receives_error_for_invalid_summary(self):
        result,scheduler,registry,*_=self.submit()
        scheduler.state='COMPLETED'
        root=Path(result['results_directory'])
        (root/'summary.json').write_text(json.dumps({'functional_status':'PASS'}))
        status=service.reconcile(result['job_id'],registry,scheduler)
        self.assertEqual(status['phase'],'error')
        self.assertEqual(status['returncode'],1)
        self.assertTrue(status['error'])

    def test_unknown_submission_stays_nonterminal_for_existing_job_poller(self):
        class Lost(fixtures.FakeScheduler):
            def submit(inner,spec): inner.specs.append(spec); raise TimeoutError('lost reply')
        result,scheduler,registry,*_=self.submit(scheduler=Lost())
        status=service.reconcile(result['job_id'],registry,scheduler)
        self.assertEqual(status['phase'],'running')
        self.assertEqual(status['work_phase'],'submission_unknown')
        self.assertEqual(status['scheduler_state'],'SUBMISSION_UNKNOWN')
        self.assertIsNone(status['scheduler_job_id'])
        self.assertEqual(len(scheduler.specs),1)
        self.assertEqual(scheduler.cancelled,[])


if __name__=='__main__': unittest.main()
