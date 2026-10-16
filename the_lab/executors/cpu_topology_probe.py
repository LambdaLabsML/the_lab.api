"""Stdlib-only, fail-closed topology check; runs before CUDA/model imports."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
import hashlib
import math

PROFILE = 'paired_24'

def cpu_list(value: str) -> set[int]:
    result = set()
    for part in value.strip().split(','):
        if not part:
            continue
        ends = part.split('-')
        if len(ends) == 1:
            result.add(int(ends[0]))
        elif len(ends) == 2 and int(ends[0]) <= int(ends[1]):
            result.update(range(int(ends[0]), int(ends[1]) + 1))
        else:
            raise ValueError('invalid kernel CPU list')
    return result

def inspect_topology(affinity: set[int], topology_root: Path, cgroup_text: str, job_id: str, *, require_job: bool = True) -> dict:
    return _inspect_topology(affinity, topology_root, cgroup_text, job_id, require_job=require_job, expected_logical=48, expected_cores=24)

def _inspect_topology(affinity, topology_root, cgroup_text, job_id, *, require_job, expected_logical, expected_cores):
    if not re.fullmatch(r'[0-9]+', job_id):
        raise ValueError('numeric Slurm job id required')
    if len(affinity) != expected_logical:
        raise ValueError(f'expected{expected_logical} logical CPUs, got{len(affinity)}')
    groups: dict[tuple[int, int], set[int]] = {}
    for cpu in sorted(affinity):
        topology = topology_root / f'cpu{cpu}' / 'topology'
        key = (int((topology/'physical_package_id').read_text()), int((topology/'core_id').read_text()))
        groups.setdefault(key, set()).add(cpu)
    if len(groups) != expected_cores or any(len(cpus) != 2 for cpus in groups.values()):
        raise ValueError('expected24 physical cores with exactly two allowed siblings each')
    for cpus in groups.values():
        for cpu in cpus:
            siblings = cpu_list((topology_root/f'cpu{cpu}'/'topology/thread_siblings_list').read_text())
            if siblings != cpus:
                raise ValueError(f'CPU{cpu} does not expose its complete two-thread sibling pair')
    paths = []
    for line in cgroup_text.splitlines():
        fields = line.split(':', 2)
        if len(fields) != 3:
            raise ValueError('invalid proc cgroup line')
        # v2 unified hierarchy, or v1 cpuset controller, must attest the job.
        if fields[0] == '0' or 'cpuset' in fields[1].split(','):
            paths.append(fields[2])
    job_component = re.compile(r'(?:^|/)job[_-]' + re.escape(job_id) + r'(?:[/.]|$)')
    if require_job and (not paths or not all(job_component.search(path) for path in paths)):
        raise ValueError('cannot attest membership of the existing Slurm job CPU cgroup')
    return {'profile': PROFILE, 'job_id': job_id, 'allowed_cpus': sorted(affinity),
            'logical_cpus': expected_logical, 'physical_cores': expected_cores, 'threads_per_core': 2,
            'core_pairs': [{'socket': key[0], 'core': key[1], 'cpus': sorted(cpus)} for key, cpus in sorted(groups.items())],
            'cpu_cgroup_paths': paths}

def verify_server_matches_batch(observed: dict, batch: dict) -> None:
    if batch.get('phase') != 'batch' or batch.get('ok') is not True:
        raise ValueError('successful batch topology receipt required')
    for key in ('profile', 'job_id', 'restart_count', 'allowed_cpus', 'core_pairs'):
        if observed.get(key) != batch.get(key):
            raise ValueError(f'server topology differs from batch receipt: {key}')

def write_receipt(path: Path, receipt: dict) -> None:
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='cpu.', delete=False) as f:
        json.dump(receipt, f, sort_keys=True); f.write('\n'); temporary = Path(f.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def canonical_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def process_identity(pid: int, proc: Path = Path('/proc')) -> dict:
    root = proc / str(pid)
    # comm may contain spaces and parentheses; field22 follows the final ')'.
    stat = (root/'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(stat[19]), 'pid_namespace': (root/'ns/pid').readlink().as_posix()}

def fresh_challenge(challenge: dict, nonce: str, job_id: str, generation: int, now: float) -> None:
    if challenge.get('nonce') != nonce or challenge.get('job_id') != job_id or challenge.get('restart_count') != generation:
        raise ValueError('challenge launch identity mismatch')
    timestamp = challenge.get('created_at')
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError('finite challenge timestamp required')
    age = now - timestamp
    if age < -2 or age > 90:
        raise ValueError('expired or future challenge')

def attest_challenge(challenge: dict, batch: dict, nonce: str, job_id: str, generation: int,
                     *, proc: Path = Path('/proc'), topology: Path = Path('/sys/devices/system/cpu'), diagnostic: dict | None = None) -> dict:
    fresh_challenge(challenge, nonce, job_id, generation, time.time())
    pid = challenge['identity']['pid']
    if type(pid) is not int or pid <= 1:
        raise ValueError('invalid probe PID')
    diagnostic = diagnostic if diagnostic is not None else {}
    identity = process_identity(pid, proc)
    diagnostic['identity'] = identity
    if identity != challenge['identity'] or identity['pid_namespace'] != (proc/'self/ns/pid').readlink().as_posix():
        raise ValueError('probe PID/starttime/namespace identity mismatch')
    cgroup = (proc/str(pid)/'cgroup').read_text()
    affinity = set(os.sched_getaffinity(pid))
    diagnostic.update(raw_cgroup=cgroup, raw_affinity=sorted(affinity), host_pid_namespace=(proc/'self/ns/pid').readlink().as_posix())
    if not re.search(r'/step_[0-9]+(?:/|$)', cgroup):
        raise ValueError('probe is not in an actual non-batch server step')
    observed = inspect_topology(affinity, topology, cgroup, job_id)
    observed['restart_count'] = generation
    verify_server_matches_batch(observed, batch)
    verify_server_matches_batch(challenge['observed'], batch)
    if process_identity(pid, proc) != identity:
        raise ValueError('probe identity changed during host attestation')
    return {'ok': True, 'phase': 'host_attestation', 'nonce': nonce, 'challenge_sha256': canonical_digest(challenge),
            'identity': identity, 'observed': observed, 'host_evidence': diagnostic, 'created_at': time.time()}

def wait_json(path: Path, deadline: float) -> dict:
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(.2)
    raise TimeoutError('timed out waiting for ' + path.name)

def run_server(args, observed: dict) -> dict:
    if args.batch_receipt is None or not args.nonce or args.handshake_dir is None:
        raise ValueError('server requires batch receipt and fresh handshake identity')
    batch = json.loads(args.batch_receipt.read_text())
    observed['restart_count'] = args.restart_count
    verify_server_matches_batch(observed, batch)
    challenge = {'nonce': args.nonce, 'job_id': args.job_id, 'restart_count': args.restart_count,
                 'created_at': time.time(), 'identity': process_identity(os.getpid()), 'observed': observed}
    write_receipt(args.handshake_dir/(args.nonce+'.challenge.json'), challenge)
    ack = wait_json(args.handshake_dir/(args.nonce+'.ack.json'), time.monotonic()+90)
    if ack.get('ok') is not True or ack.get('nonce') != args.nonce or ack.get('challenge_sha256') != canonical_digest(challenge):
        raise ValueError('host rejected or mismatched probe challenge')
    if ack.get('identity') != process_identity(os.getpid()) or not -2 <= time.time()-ack['created_at'] <= 90:
        raise ValueError('stale host acknowledgment or process identity')
    verify_server_matches_batch(ack['observed'], batch)
    current = inspect_topology(set(os.sched_getaffinity(0)), Path('/sys/devices/system/cpu'), Path('/proc/self/cgroup').read_text(), args.job_id, require_job=False)
    current['restart_count'] = args.restart_count
    verify_server_matches_batch(current, batch)
    return {**observed, 'host_attestation': ack}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('batch', 'server', 'host'), required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--restart-count', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batch-receipt', type=Path)
    parser.add_argument('--handshake-dir', type=Path)
    parser.add_argument('--nonce')
    parser.add_argument('--launcher-pid', type=int)
    args = parser.parse_args()
    observed = {}
    try:
        if args.restart_count < 0:
            raise ValueError('nonnegative restart count required')
        if args.phase != 'batch' and not re.fullmatch(r'[0-9a-f]{64}', args.nonce or ''):
            raise ValueError('fresh 256-bit launch nonce required')
        if args.phase == 'host':
            if args.handshake_dir is None or args.batch_receipt is None or args.launcher_pid is None:
                raise ValueError('host requires handshake directory, batch receipt and live launcher PID')
            deadline = time.monotonic()+90
            challenge_path = args.handshake_dir/(args.nonce+'.challenge.json')
            while not challenge_path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('server challenge deadline expired')
                os.kill(args.launcher_pid, 0)
                if Path('/proc', str(args.launcher_pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                    raise RuntimeError('server srun exited before challenge')
                time.sleep(.2)
            challenge = wait_json(challenge_path, deadline)
            observed = {'challenge': challenge, 'host_observations': {}}
            observed = attest_challenge(challenge, json.loads(args.batch_receipt.read_text()), args.nonce, args.job_id, args.restart_count, diagnostic=observed['host_observations'])
            write_receipt(args.handshake_dir/(args.nonce+'.ack.json'), observed)
            receipt = observed
        else:
            # Preserve raw namespace/process observations even when geometry rejects.
            observed = {'identity': process_identity(os.getpid()), 'raw_cgroup': Path('/proc/self/cgroup').read_text(),
                        'cgroup_namespace': Path('/proc/self/ns/cgroup').readlink().as_posix(),
                        'raw_affinity': sorted(os.sched_getaffinity(0))}
            observed.update(inspect_topology(set(observed['raw_affinity']), Path('/sys/devices/system/cpu'), observed['raw_cgroup'], args.job_id, require_job=args.phase=='batch'))
            observed['restart_count'] = args.restart_count
            receipt = run_server(args, observed) if args.phase == 'server' else observed
        receipt.update(phase=args.phase, ok=True)
    except Exception as error:
        failure = {'profile': PROFILE, 'phase': args.phase, 'job_id': args.job_id,
                   'restart_count': args.restart_count, 'ok': False, 'error': f'{type(error).__name__}: {error}', 'observed': observed}
        write_receipt(args.output, failure)
        if args.phase == 'host' and args.handshake_dir and args.nonce:
            write_receipt(args.handshake_dir/(args.nonce+'.ack.json'), failure)
        raise SystemExit(f'CPU profile rejected before model startup: {error}')
    write_receipt(args.output, receipt)
    print(json.dumps(receipt, sort_keys=True), flush=True)

if __name__ == '__main__':
    main()
