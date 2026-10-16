"""CPU-only fake-topology/rendering checks; no Slurm/model/API execution."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from the_lab.executors.cpu_profile import validate_cpu_profile, directives, render_setup
from the_lab.executors.cpu_topology_probe import cpu_list, inspect_topology, verify_server_matches_batch
from the_lab.executors import cpu_topology_probe as probe
from the_lab.executors.slurm import SlurmExecutor, is_per_attempt_file
from the_lab.queue import _validate_executor_config

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT/'the_lab/artifacts/model_inference/launch_qwen38_flash_sglang.sh'
CONFIG = {'cpu_profile': 'paired_24', 'cpus_per_task': 48, 'threads_per_core': 2, 'cpu_bind': 'cores'}
BASE = '97c2ad2722d08285c053906052dbcb4ff6baba64'

class CpuProfileConfigurationTests(unittest.TestCase):
    def test_opt_in_is_required(self):
        self.assertIsNone(validate_cpu_profile({}))
        self.assertEqual(directives(None), '')
        self.assertEqual(render_setup(None, '/tmp/jobs'), '')
        for key, value in [('cpus_per_task',48), ('threads_per_core',2), ('cpu_bind','cores')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                _validate_executor_config({key:value})

    def test_complete_resource_profile_is_valid(self):
        _validate_executor_config(CONFIG)
        self.assertEqual(validate_cpu_profile(CONFIG), 'paired_24')

    def test_positive_integer_and_fixed_pair_validation(self):
        for key in ('cpus_per_task','threads_per_core'):
            for value in (True,False,0,-1,1.0,'48',None):
                with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                    _validate_executor_config({**CONFIG,key:value})
        for key,value in [('cpus_per_task',24),('threads_per_core',1),('cpu_bind','threads'),('cpu_bind','none'),('cpu_bind','cores;echo injected'),('cpu_profile','arbitrary'),('ntasks',2),('ntasks',True),('ntasks',1.0)]:
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                SlurmExecutor('local',{**CONFIG,key:value})

    def test_cpu_directives_and_gate_are_before_model(self):
        ex=SlurmExecutor('local',{**CONFIG,'model_server':{'kind':'qwen38_flash_sglang'}})
        wrapper=ex._build_wrapper('7.1','/tmp/jobs/7.1','/tmp/worktree')
        for expected in ('#SBATCH --nodes=1','#SBATCH --ntasks=1','#SBATCH --cpus-per-task=48','#SBATCH --threads-per-core=2'):
            self.assertIn(expected,wrapper)
        self.assertLess(wrapper.index('--phase batch'),wrapper.index('# ── allocation-local Qwen'))
        self.assertLess(wrapper.index('--phase batch'),wrapper.index('bash "/tmp/jobs/7.1/script.sh"'))
        self.assertIn('SLURM_RESTART_COUNT:-0',wrapper)
        self.assertIn('"/tmp/jobs/7.1"/cpu_profile',wrapper)
        self.assertTrue(is_per_attempt_file('cpu_profile'))
        with tempfile.NamedTemporaryFile('w',suffix='.sh')as f:
            f.write(wrapper);f.flush();subprocess.run(['bash','-n',f.name],check=True,capture_output=True)

    def test_default_wrapper_byte_parity_with_pinned_base(self):
        # Load only the pinned renderer implementation, never execute a wrapper.
        source=subprocess.check_output(['git','show',f'{BASE}:the_lab/executors/slurm.py'],cwd=ROOT,text=True)
        with tempfile.TemporaryDirectory()as tmp:
            p=Path(tmp)/'baseline.py';p.write_text(source)
            spec=importlib.util.spec_from_file_location('the_lab.executors._cpu_profile_baseline',p)
            old=importlib.util.module_from_spec(spec);sys.modules[spec.name]=old;spec.loader.exec_module(old)
            cases=[{}, {'mem':'160G','time':'10:00:00','qos':'short','base_venv_path':'/shared/venv'}, {'model_server':{'kind':'qwen38_flash_sglang','model_inference_root':'/shared/models'}}]
            for cfg in cases:
                args=('7.1','/tmp/jobs/7.1','/tmp/worktree')
                kw={'env_vars':{'SAFE_TEST':'yes'},'requires_preamble':True,'requires_experiment_json':True,'source_guard':'true'}
                with self.subTest(config=cfg):
                    self.assertEqual(old.SlurmExecutor('local',cfg)._build_wrapper(*args,**kw), SlurmExecutor('local',cfg)._build_wrapper(*args,**kw))

class FakeTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.affinity=set(range(48));self.cgroup='0::/system.slice/slurmstepd.scope/job_123/step_batch\n'
        for cpu in range(48):
            p=self.root/f'cpu{cpu}'/'topology';p.mkdir(parents=True)
            (p/'physical_package_id').write_text(str(cpu//24));(p/'core_id').write_text(str((cpu%24)//2));(p/'thread_siblings_list').write_text(f'{cpu//2*2}-{cpu//2*2+1}\n')

    def observed(self):return inspect_topology(self.affinity,self.root,self.cgroup,'123')

    def test_valid_48_threads_are_24_complete_pairs(self):
        x=self.observed();self.assertEqual(x['logical_cpus'],48);self.assertEqual(x['physical_cores'],24)
        self.assertEqual(len(x['core_pairs']),24)

    def test_wrong_logical_count_rejected(self):
        for n in (20,24,47,49):
            with self.subTest(n=n),self.assertRaises(ValueError):inspect_topology(set(range(n)),self.root,self.cgroup,'123')

    def test_48_distinct_cores_are_not_24_pairs(self):
        for cpu in self.affinity:(self.root/f'cpu{cpu}/topology/core_id').write_text(str(cpu))
        with self.assertRaises(ValueError):self.observed()

    def test_partial_sibling_pairs_rejected(self):
        (self.root/'cpu0/topology/thread_siblings_list').write_text('0,99')
        with self.assertRaises(ValueError):self.observed()

    def test_missing_topology_fails_closed(self):
        (self.root/'cpu0/topology/core_id').unlink()
        with self.assertRaises(OSError):self.observed()

    def test_other_job_or_hidden_cgroup_fails_closed(self):
        for group in ('0::/\n','0::/job_1234/step_batch\n','0::/job_999/step_batch\n','2:memory:/job_123/step_batch\n'):
            with self.subTest(group=group),self.assertRaises(ValueError):inspect_topology(self.affinity,self.root,group,'123')

    def test_cpuset_v1_and_v2_both_supported(self):
        self.assertEqual(inspect_topology(self.affinity,self.root,'2:cpuset:/slurm/uid_1/job_123/step_batch\n4:memory:/other\n','123')['job_id'],'123')

    def test_server_must_match_successful_batch_and_generation(self):
        x=self.observed();x['restart_count']=2;batch={**x,'phase':'batch','ok':True}
        verify_server_matches_batch(x,batch)
        for key,value in [('job_id','999'),('allowed_cpus',list(range(48,96))),('restart_count',1),('phase','server'),('ok',False)]:
            with self.subTest(key=key),self.assertRaises(ValueError):verify_server_matches_batch(x,{**batch,key:value})

    def test_kernel_cpu_list(self):
        self.assertEqual(cpu_list('0-2,5,7-8'),{0,1,2,5,7,8})
        with self.assertRaises(ValueError):cpu_list('4-2')

    def test_main_retains_failure_receipt(self):
        out=self.root/'failure.json'
        args=['probe','--phase','batch','--job-id','123','--restart-count','0','--output',str(out)]
        with patch.object(sys,'argv',args),patch.object(probe,'inspect_topology',side_effect=ValueError('wrong mask')),self.assertRaises(SystemExit):probe.main()
        r=json.loads(out.read_text());self.assertFalse(r['ok']);self.assertIn('wrong mask',r['error']);self.assertEqual(r['restart_count'],0)

    def test_main_server_receipt_requires_matching_batch(self):
        out=self.root/'server.json';batch=self.root/'batch.json';x=self.observed();batch.write_text(json.dumps({**x,'phase':'batch','ok':True,'restart_count':1}))
        args=['probe','--phase','server','--nonce','a'*64,'--job-id','123','--restart-count','1','--batch-receipt',str(batch),'--output',str(out)]
        with patch.object(sys,'argv',args),patch.object(probe,'inspect_topology',return_value=x),patch.object(probe,'run_server',return_value=x),patch.object(probe,'process_identity',return_value={}):probe.main()
        self.assertTrue(json.loads(out.read_text())['ok'])

class LauncherWiringTests(unittest.TestCase):
    def source(self):return LAUNCHER.read_text()
    def profile_block(self):
        s=self.source();return s[s.index('CPU_ARGS=()'):s.index('srun "${CPU_ARGS[@]}"')]
    def test_shell_syntax(self):subprocess.run(['bash','-n',str(LAUNCHER)],check=True,capture_output=True)
    def test_default_off_adds_no_cpu_arguments(self):
        result=subprocess.run(['bash','-euc',self.profile_block()+'\nprintf "%s" "${CPU_ARGS[*]}"'],env={'PATH':os.environ['PATH']},capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout,'')
    def test_profile_has_explicit_server_step_args_and_mounts(self):
        with tempfile.TemporaryDirectory()as tmp:
            p=Path(tmp);code=p/'probe.py';code.write_text('not executed');(p/'batch.json').write_text('{}')
            env={**os.environ,'SLURM_JOB_ID':'123','THE_LAB_CPU_PROFILE':'paired_24','THE_LAB_CPU_PROFILE_GENERATION':'0','THE_LAB_CPU_PROFILE_JOB_ID':'123','THE_LAB_CPU_PROBE_PATH':str(code),'THE_LAB_CPU_PROBE_SHA256':hashlib.sha256(code.read_bytes()).hexdigest(),'THE_LAB_CPU_RECEIPT_DIR':str(p),'CONTAINER_ENVS':'OLD','CONTAINER_MOUNTS':'OLD'}
            r=subprocess.run(['bash','-euc',self.profile_block()+'\nprintf "%s\\n%s" "${CPU_ARGS[*]}" "$CONTAINER_MOUNTS"'],env=env,text=True,capture_output=True,check=True)
            self.assertIn('--cpus-per-task=48',r.stdout);self.assertIn('--threads-per-core=2',r.stdout);self.assertIn('--cpu-bind=verbose,cores',r.stdout);self.assertIn('/the_lab_cpu_probe.py:ro',r.stdout)
    def test_missing_batch_or_unknown_profile_fails(self):
        for profile in ('unknown','paired_24'):
            r=subprocess.run(['bash','-euc',self.profile_block()],env={'PATH':os.environ['PATH'],'THE_LAB_CPU_PROFILE':profile},capture_output=True)
            self.assertNotEqual(r.returncode,0)
    def test_actual_container_command_gate_precedes_model_and_fails_closed(self):
        # Execute only the real gate prefix with a fake python executable; no srun/model.
        s=self.source();start=s.index("bash -lc '")+len("bash -lc '");gate=s[start:s.index('    export HF_HOME=',start)]
        self.assertIn('--phase server',gate);self.assertIn('--batch-receipt /the_lab_cpu_receipts/batch.json',gate)
        with tempfile.TemporaryDirectory()as tmp:
            p=Path(tmp)/'python3';p.write_text('#!/bin/sh\nexit "${FAKE_GATE_EXIT:-0}"\n');p.chmod(0o755)
            for code in ('0','1'):
                env={**os.environ,'PATH':tmp+':'+os.environ['PATH'],'THE_LAB_CPU_PROFILE':'paired_24','THE_LAB_CPU_PROFILE_JOB_ID':'123','THE_LAB_CPU_PROFILE_GENERATION':'0','FAKE_GATE_EXIT':code}
                r=subprocess.run(['bash','-c',gate+'echo AFTER_GATE'],env=env,text=True,capture_output=True)
                self.assertEqual(r.returncode,int(code));self.assertEqual('AFTER_GATE'in r.stdout,code=='0')

class HandshakeTests(FakeTopologyTests):
    def challenge(self):
        x=self.observed();x['restart_count']=0
        return {'nonce':'a'*64,'job_id':'123','restart_count':0,'created_at':probe.time.time(),'identity':{'pid':42,'start_ticks':900,'pid_namespace':'pid:[1]'},'observed':x}
    def test_reject_stale_wrong_identity(self):
        c=self.challenge()
        for key,value in [('nonce','b'*64),('job_id','999'),('restart_count',1),('created_at',probe.time.time()-100),('created_at',probe.time.time()+5),('created_at',float('nan')),('created_at',float('inf')),('created_at',True)]:
            with self.subTest(key=key),self.assertRaises(ValueError):probe.fresh_challenge({**c,key:value},'a'*64,'123',0,probe.time.time())
    def test_host_attests_actual_process_and_matching_batch(self):
        proc=self.root/'proc';(proc/'42/ns').mkdir(parents=True);(proc/'self/ns').mkdir(parents=True)
        (proc/'42/ns/pid').symlink_to('pid:[1]');(proc/'self/ns/pid').symlink_to('pid:[1]')
        (proc/'42/stat').write_text('42 (name with ) spaces) '+' '.join(['S']+['0']*18+['900']))
        (proc/'42/cgroup').write_text('0::/slurm/job_123/step_7/user/task_0\n')
        c=self.challenge();batch={**c['observed'],'phase':'batch','ok':True}
        with patch.object(probe.os,'sched_getaffinity',return_value=self.affinity):
            ack=probe.attest_challenge(c,batch,'a'*64,'123',0,proc=proc,topology=self.root)
            self.assertTrue(ack['ok']);self.assertEqual(ack['challenge_sha256'],probe.canonical_digest(c))
            for group in ('0::/\n','0::/job_999/step_7\n','0::/job_123/step_batch\n'):
                (proc/'42/cgroup').write_text(group)
                with self.subTest(group=group),self.assertRaises(ValueError):probe.attest_challenge(c,batch,'a'*64,'123',0,proc=proc,topology=self.root)
            (proc/'42/cgroup').write_text('0::/job_123/step_7\n')
            for bad in ({**c,'identity':{**c['identity'],'start_ticks':901}}, {**c,'observed':{**c['observed'],'allowed_cpus':list(range(48,96))}}):
                with self.assertRaises(ValueError):probe.attest_challenge(bad,batch,'a'*64,'123',0,proc=proc,topology=self.root)
    def test_missing_ack_is_bounded(self):
        with self.assertRaises(TimeoutError):probe.wait_json(self.root/'missing.json',probe.time.monotonic())
    def test_reserved_nonce_required(self):
        args=['probe','--phase','host','--nonce','stale','--job-id','123','--restart-count','0','--output',str(self.root/'bad.json')]
        with patch.object(sys,'argv',args),self.assertRaises(SystemExit):probe.main()
        self.assertFalse(json.loads((self.root/'bad.json').read_text())['ok'])

    def test_namespace_relative_view_requires_host_attestation(self):
        x=inspect_topology(self.affinity,self.root,'0::/\n','123',require_job=False)
        self.assertEqual(x['logical_cpus'],48)
        self.assertEqual(x['cpu_cgroup_paths'],['/'])
        with self.assertRaises(ValueError):inspect_topology(self.affinity,self.root,'0::/\n','123')
    def test_production_counts_are_not_configurable(self):
        import inspect
        self.assertNotIn('expected_logical',inspect.signature(inspect_topology).parameters)
        with self.assertRaises(ValueError):inspect_topology({0,1},self.root,self.cgroup,'123',require_job=False)
    def test_host_failure_retains_raw_process_observations(self):
        proc=self.root/'proc';(proc/'42/ns').mkdir(parents=True);(proc/'self/ns').mkdir(parents=True)
        (proc/'42/ns/pid').symlink_to('pid:[1]');(proc/'self/ns/pid').symlink_to('pid:[1]')
        (proc/'42/stat').write_text('42 (probe) '+' '.join(['S']+['0']*18+['900']))
        (proc/'42/cgroup').write_text('0::/job_999/step_7\n')
        c=self.challenge();diagnostic={}
        with patch.object(probe.os,'sched_getaffinity',return_value=self.affinity),self.assertRaises(ValueError):
            probe.attest_challenge(c,{**c['observed'],'phase':'batch','ok':True},'a'*64,'123',0,proc=proc,topology=self.root,diagnostic=diagnostic)
        self.assertEqual(diagnostic['raw_cgroup'],'0::/job_999/step_7\n')
        self.assertEqual(diagnostic['raw_affinity'],list(range(48)))
    def test_server_rechecks_affinity_after_ack_and_rejects_bad_ack(self):
        from types import SimpleNamespace
        c=self.challenge();x=c['observed'];batch=self.root/'batch.json';batch.write_text(json.dumps({**x,'phase':'batch','ok':True}))
        args=SimpleNamespace(batch_receipt=batch,nonce='a'*64,handshake_dir=self.root,restart_count=0,job_id='123')
        identity=c['identity']
        def ack(path,deadline):
            challenge=json.loads((self.root/('a'*64+'.challenge.json')).read_text())
            return {'ok':True,'nonce':'a'*64,'challenge_sha256':probe.canonical_digest(challenge),'identity':identity,'created_at':probe.time.time(),'observed':x}
        with patch.object(probe,'process_identity',return_value=identity),patch.object(probe,'wait_json',side_effect=ack),patch.object(probe,'inspect_topology',return_value={**x,'allowed_cpus':list(range(48,96))}),self.assertRaises(ValueError):
            probe.run_server(args,x)
        with patch.object(probe,'process_identity',return_value=identity),patch.object(probe,'wait_json',return_value={'ok':True,'nonce':'a'*64,'challenge_sha256':'stale'}),self.assertRaises(ValueError):
            probe.run_server(args,x)
    def test_server_rejects_wrong_nonce_digest_and_stale_ack(self):
        from types import SimpleNamespace
        c=self.challenge();x=c['observed'];batch=self.root/'batch.json';batch.write_text(json.dumps({**x,'phase':'batch','ok':True}))
        args=SimpleNamespace(batch_receipt=batch,nonce='a'*64,handshake_dir=self.root,restart_count=0,job_id='123')
        identity=c['identity']
        for key,value in [('nonce','b'*64),('challenge_sha256','bad'),('created_at',probe.time.time()-100),('created_at',float('nan'))]:
            def ack(path,deadline):
                challenge=json.loads((self.root/('a'*64+'.challenge.json')).read_text())
                return {**{'ok':True,'nonce':'a'*64,'challenge_sha256':probe.canonical_digest(challenge),'identity':identity,'created_at':probe.time.time(),'observed':x},key:value}
            with self.subTest(key=key,value=value),patch.object(probe,'process_identity',return_value=identity),patch.object(probe,'wait_json',side_effect=ack),self.assertRaises(ValueError):probe.run_server(args,x)
    def test_host_stops_waiting_for_dead_or_zombie_launcher(self):
        for dead in (True,False):
            out=self.root/('dead.json' if dead else 'zombie.json')
            args=['probe','--phase','host','--nonce','a'*64,'--job-id','123','--restart-count','0','--output',str(out),'--batch-receipt',str(self.root/'batch.json'),'--handshake-dir',str(self.root),'--launcher-pid','987654']
            with patch.object(sys,'argv',args),patch.object(probe.os,'kill',side_effect=ProcessLookupError('exited') if dead else None),patch.object(Path,'read_text',return_value='987654 (srun) Z'),self.assertRaises(SystemExit):probe.main()
            self.assertFalse(json.loads(out.read_text())['ok'])
