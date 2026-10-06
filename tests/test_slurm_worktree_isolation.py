"""Real Git and subprocess regressions. No scheduler/network/model calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from the_lab.executors.slurm import SlurmExecutor
from the_lab.executors.worktree_attestation import attest


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    git(source, 'init', '-q'); git(source, 'config', 'user.email', 'test@example.invalid'); git(source, 'config', 'user.name', 'test')
    (source / 'solver.py').write_text('BASE\n')
    (source / 'link').symlink_to('solver.py')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'base')
    base = git(source, 'rev-parse', 'HEAD')
    (source / 'solver.py').write_text('CANDIDATE\n')
    git(source, 'commit', '-qam', 'candidate'); candidate = git(source, 'rev-parse', 'HEAD')
    exe = SlurmExecutor('local', {'git_repo_path': str(tmp_path/'bare.git'), 'remote_base': str(tmp_path/'jobs')})
    bare = exe.ensure_bare_repo(); exe.push_branch(source, git(source, 'branch', '--show-current'))
    return exe, source, bare, base, candidate


def add(setup, label, commit):
    exe, source, bare, _, _ = setup
    wt = Path(exe.remote_base) / label / 'worktree'; wt.parent.mkdir(parents=True)
    exe.create_worktree(bare, str(wt), commit)
    return wt


def test_reciprocal_collision_despite_candidate_bytes(setup):
    exe, source, bare, base, candidate = setup
    control, treatment = add(setup, 'control', base), add(setup, 'candidate', candidate)
    assert attest(bare, str(treatment), candidate)['qualified']
    (treatment/'.git').write_bytes((control/'.git').read_bytes())
    assert git(treatment, 'rev-parse', 'HEAD') == base
    assert (treatment/'solver.py').read_text() == 'CANDIDATE\n'
    # Even the apparent HEAD cannot make an aliased candidate qualify as base.
    for expected in (base, candidate):
        with pytest.raises(ValueError, match='nonreciprocal'):
            attest(bare, str(treatment), expected)


def test_tracked_bytes_not_index_assume_unchanged(setup):
    exe, source, bare, base, _ = setup; wt = add(setup, 'a', base)
    git(wt, 'update-index', '--assume-unchanged', 'solver.py')
    (wt/'solver.py').write_text('WRONG\n')
    assert git(wt, 'diff', base) == ''
    with pytest.raises(ValueError, match='tracked bytes differ'):
        attest(bare, str(wt), base)


def test_add_preserves_existing_target_and_foreign_index_lock(setup):
    exe, source, bare, base, candidate = setup; wt = add(setup, 'a', base)
    admin = Path((wt/'.git').read_text().strip()[8:]); lock = admin/'index.lock'; lock.write_text('active operation')
    with pytest.raises(RuntimeError): exe.create_worktree(bare, str(wt), candidate)
    add(setup, 'b', candidate)
    assert lock.read_text() == 'active operation'
    assert attest(bare, str(wt), base)['qualified']
    git(Path(bare), 'worktree', 'prune', '--expire=now')
    assert (admin/'locked').is_file()


def submit_fake(setup, label='1.1'):
    exe, source, bare, base, candidate = setup
    script = source/'.the_lab/experiments'/label/'script.sh'; script.parent.mkdir(parents=True); script.write_text('echo solver\n')
    with patch.object(exe, '_ssh', return_value=subprocess.CompletedProcess([], 0, '123\n', '')) as scheduler:
        assert exe.submit(label, script, {}, script.parent, local_repo_dir=source,
                          git_branch=git(source,'branch','--show-current'), git_commit=base,
                          experiment_record={'meta':{'git_commit':base}}) == '123'
    assert scheduler.call_count == 1
    return Path(exe.remote_base)/label


def test_submit_and_cleanup_preserve_receipts(setup):
    exe, source, bare, base, candidate = setup; root = submit_fake(setup)
    assert json.loads((root/'source-attestation-submit.json').read_text())['qualified']
    assert attest(bare, str(root/'worktree'), base)['qualified']
    (root/'script.log').write_text('failure evidence')
    exe.cleanup_remote('1.1', bare)
    assert not (root/'worktree').exists()
    assert (root/'script.log').read_text() == 'failure evidence'
    assert len(list(root.glob('source-attestation-cleanup-*.json'))) == 1


def test_cleanup_collision_preserves_both_registrations_and_failure_receipt(setup):
    exe, source, bare, base, candidate = setup; root = submit_fake(setup)
    other = add(setup, 'other', candidate)
    (root/'worktree/.git').write_bytes((other/'.git').read_bytes())
    with pytest.raises(RuntimeError): exe.cleanup_remote('1.1', bare)
    assert other.exists() and (root/'worktree').exists()
    receipt = json.loads(next(root.glob('source-attestation-cleanup-*.json')).read_text())
    assert receipt['qualified'] is False and 'nonreciprocal' in receipt['error']
    assert attest(bare, str(other), candidate)['qualified']


def test_wrapper_checks_before_runtime_and_requeue_has_new_receipt(setup):
    exe, source, bare, base, candidate = setup; root = submit_fake(setup)
    wrapper = (root/'wrapper.sh').read_text()
    assert wrapper.index('source-attestation-prestartup-') < wrapper.index('# Progress/metrics')
    prefix = wrapper.split('# Progress/metrics')[0] + '\necho REACHED_RUNTIME\n'
    for generation in (0,1):
        result = subprocess.run(['bash','-c',prefix],env={**os.environ,'SLURM_RESTART_COUNT':str(generation)},capture_output=True,text=True)
        assert result.returncode == 0, result.stderr
        assert 'REACHED_RUNTIME' in result.stdout
    (root/'worktree/solver.py').write_text('CORRUPT\n')
    result = subprocess.run(['bash','-c',prefix],env={**os.environ,'SLURM_RESTART_COUNT':'2'},capture_output=True,text=True)
    assert result.returncode == 125
    assert 'REACHED_RUNTIME' not in result.stdout
    assert json.loads((root/'source-attestation-prestartup-2.json').read_text())['qualified'] is False


def test_stale_lock_is_not_stolen(setup):
    exe, source, bare, base, candidate = setup
    lock = Path(bare+'.the-lab-lifecycle-lock'); lock.mkdir(); (lock/'owner').write_text('another-owner')
    with patch('the_lab.executors.slurm.time.monotonic',side_effect=[0,121]):
        with pytest.raises(RuntimeError,match='no automatic stale-lock removal'):
            exe.create_worktree(bare,str(Path(exe.remote_base)/'never'),base)
    assert (lock/'owner').read_text() == 'another-owner'


def test_cross_process_lifecycle_serialization(setup, tmp_path):
    exe, source, bare, base, candidate = setup
    code = '''import sys,time
from pathlib import Path
from the_lab.executors.slurm import SlurmExecutor
x=SlurmExecutor('local',{'git_repo_path':sys.argv[1]})
with x._git_lifecycle():
 p=Path(sys.argv[2]); p.write_text('entered'); time.sleep(float(sys.argv[3]))
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    first=tmp_path/'first'; second=tmp_path/'second'
    p=subprocess.Popen([sys.executable,'-c',code,bare,str(first),'1.5'],env=env)
    deadline=time.monotonic()+10
    while not first.exists() and time.monotonic()<deadline: time.sleep(.02)
    assert first.exists()
    q=subprocess.Popen([sys.executable,'-c',code,bare,str(second),'0'],env=env)
    time.sleep(.4); assert not second.exists()
    assert p.wait(timeout=10)==0 and q.wait(timeout=10)==0
    assert second.exists() and not Path(bare+'.the-lab-lifecycle-lock').exists()


def test_real_prune_slot_reuse_and_persistent_lock_prevention(setup, tmp_path):
    exe, source, bare, base, candidate = setup
    # Model transient absence of an allocation directory. With the old unlocked
    # policy Git can recycle its admin slot while its bytes still exist elsewhere.
    old = Path(exe.remote_base)/'old'/'worktree'; old.parent.mkdir(parents=True)
    git(Path(bare),'worktree','add','--detach',str(old),candidate)
    old_pointer=(old/'.git').read_text(); hidden=tmp_path/'hidden'; old.rename(hidden)
    git(Path(bare),'worktree','prune','--expire=now')
    new = add(setup,'replacement',base)
    hidden.rename(old)
    assert (new/'.git').read_text() == old_pointer
    assert git(old,'rev-parse','HEAD') == base
    assert (old/'solver.py').read_text() == 'CANDIDATE\n'
    with pytest.raises(ValueError,match='nonreciprocal'):
        attest(bare,str(old),base)
    # New persistent lock prevents this same native Git prune/reuse sequence.
    kept = add(setup,'kept',candidate); kept_pointer=(kept/'.git').read_text()
    kept.rename(hidden)
    git(Path(bare),'worktree','prune','--expire=now')
    another=add(setup,'another',base)
    hidden.rename(kept)
    assert (another/'.git').read_text() != kept_pointer
    assert attest(bare,str(kept),candidate)['qualified']


def test_conflicting_frozen_source_never_submits(setup):
    exe, source, bare, base, candidate=setup
    script=source/'run.sh'; script.write_text('echo never\n')
    with patch.object(exe,'_ssh') as scheduler:
        with pytest.raises(RuntimeError,match='conflicts with checkout commit'):
            exe.submit('wrong',script,{},source,local_repo_dir=source,
                       git_branch=git(source,'branch','--show-current'),git_commit=base,
                       experiment_record={'meta':{'EXPECTED_COMMIT':candidate}})
        scheduler.assert_not_called()


def test_release_never_deletes_another_owner_or_masks_primary(setup):
    exe, source, bare, base, candidate=setup
    lock=Path(bare+'.the-lab-lifecycle-lock')
    with pytest.raises(ValueError,match='original failure'):
        with exe._git_lifecycle():
            (lock/'owner').write_text('replacement-owner')
            raise ValueError('original failure')
    assert (lock/'owner').read_text() == 'replacement-owner'


def test_preexec_failure_explicitly_reaps_owned_children(tmp_path):
    exe=SlurmExecutor('local',{})
    wrapper=exe._build_wrapper('test',str(tmp_path),str(tmp_path),source_guard='false')
    recheck=wrapper.split('# Recheck after runtime setup;')[1].split('# Run the experiment script')[0]
    recheck=recheck.split('\n',1)[1]
    model_marker=tmp_path/'model-reaped'
    shell=f'''sleep 600 &
_WATCHER_PID=$!
sleep 600 &
_MODEL_PID=$!
echo "CHILDREN=$_WATCHER_PID $_MODEL_PID"
_cleanup_model() {{ kill "$_MODEL_PID"; wait "$_MODEL_PID" 2>/dev/null || true; touch "{model_marker}"; }}
{recheck}
echo MUST_NOT_EXECUTE
'''
    result=subprocess.run(['bash','-c',shell],capture_output=True,text=True,timeout=10)
    assert result.returncode==125
    assert 'MUST_NOT_EXECUTE' not in result.stdout and model_marker.exists()
    pids=result.stdout.split('CHILDREN=')[1].splitlines()[0].split()
    for pid in pids:
        with pytest.raises(ProcessLookupError): os.kill(int(pid),0)


@pytest.mark.parametrize('ancestor', [False, True])
def test_requested_root_and_ancestor_alias_never_qualify(setup, tmp_path, ancestor):
    exe, source, bare, base, candidate=setup; wt=add(setup,'actual',base)
    alias=tmp_path/'alias'
    alias.symlink_to(wt.parent if ancestor else wt, target_is_directory=True)
    requested=alias/'worktree' if ancestor else alias
    with pytest.raises(ValueError,match='path alias'):
        attest(bare,str(requested),base)
    assert attest(bare,str(wt),base)['qualified']


def test_admin_symlink_alias_never_qualifies(setup):
    exe, source, bare, base, candidate=setup; wt=add(setup,'actual',base)
    admin=Path((wt/'.git').read_text().strip()[8:])
    alias=admin.parent/'alias'; alias.symlink_to(admin,target_is_directory=True)
    (wt/'.git').write_text('gitdir: '+str(alias)+'\n')
    with pytest.raises(ValueError,match='administrative directory'):
        attest(bare,str(wt),base)


@pytest.mark.parametrize('check',[True,False])
def test_timeout_descendant_cannot_overlap_second_owner(setup, tmp_path, check):
    from the_lab.executors.slurm import UncertainCommandCompletion
    import shlex
    exe, source, bare, base, candidate=setup; marker=tmp_path/'child-finished'
    started=tmp_path/'child-started'
    code='import time;from pathlib import Path;Path('+repr(str(started))+').write_text("started");time.sleep(1);Path('+repr(str(marker))+').write_text("done")'
    def timeout_after_child_started(*args, **kwargs):
        # Match subprocess.run's immediate-parent kill while guaranteeing that
        # the descendant already exists; a short login-shell timeout is racy.
        kwargs.pop('timeout',None); kwargs.pop('capture_output',None)
        proc=subprocess.Popen(*args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs)
        end=time.monotonic()+5
        while not started.exists() and time.monotonic()<end:time.sleep(.01)
        assert started.exists()
        proc.kill(); proc.wait(timeout=5)
        raise subprocess.TimeoutExpired(args[0],.1)
    with pytest.raises(UncertainCommandCompletion):
        with exe._git_lifecycle():
            with patch('the_lab.executors.slurm.subprocess.run',side_effect=timeout_after_child_started):
                exe._ssh_plain(shlex.quote(sys.executable)+' -c '+shlex.quote(code)+'; :',timeout=.1,check=check)
    lock=Path(bare+'.the-lab-lifecycle-lock')
    assert lock.is_dir()
    with patch('the_lab.executors.slurm.time.monotonic',side_effect=[0,121]):
        with pytest.raises(RuntimeError,match='no automatic stale-lock removal'):
            with exe._git_lifecycle(): pytest.fail('second owner must never acquire')
    end=time.monotonic()+4
    while not marker.exists() and time.monotonic()<end: time.sleep(.03)
    assert marker.exists() and lock.is_dir()


def test_ssh_disconnect_is_uncertain_even_check_false(setup):
    from the_lab.executors.slurm import UncertainCommandCompletion
    exe, source, bare, base, candidate=setup
    # Only test result classification, no SSH connection is made.
    exe._is_local=False
    with patch('the_lab.executors.slurm.subprocess.run',return_value=subprocess.CompletedProcess([],255,'','disconnected')):
        for method in (exe._ssh_plain,exe._ssh):
            with pytest.raises(UncertainCommandCompletion):method('true',check=False)


@pytest.mark.parametrize('which',['worktree','bare','admin'])
def test_dot_parent_paths_are_rejected_before_normalization(setup, which):
    exe, source, bare, base, candidate=setup;wt=add(setup,'canonical',base)
    if which=='admin':
        admin=Path((wt/'.git').read_text().strip()[8:])
        (wt/'.git').write_text('gitdir: '+str(admin.parent)+'/alias/../'+admin.name+'\n')
        with pytest.raises(ValueError,match='noncanonical administrative'):
            attest(bare,str(wt),base)
    else:
        root_arg=str(wt.parent)+'/alias/../worktree' if which=='worktree' else str(wt)
        bare_arg=str(Path(bare).parent)+'/alias/../bare.git' if which=='bare' else bare
        with pytest.raises(ValueError,match='noncanonical requested'):
            attest(bare_arg,root_arg,base)
