"""Explicit resource-only CPU profile. Absent configuration preserves legacy output."""
from pathlib import Path
import hashlib
import shlex

PROFILE = 'paired_24'
FIELDS = ('cpus_per_task', 'threads_per_core', 'cpu_bind')

def validate_cpu_profile(config: dict) -> str | None:
    profile = config.get('cpu_profile')
    if profile is None:
        if any(config.get(k) is not None for k in FIELDS):
            raise ValueError('CPU allocation fields require cpu_profile=paired_24')
        return None
    if profile != PROFILE:
        raise ValueError('executor_config.cpu_profile must be paired_24 or null')
    for key in ('cpus_per_task', 'threads_per_core'):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'executor_config.{key} must be a positive integer')
    if config.get('cpu_bind') not in ('cores', 'threads'):
        raise ValueError('executor_config.cpu_bind must be cores or threads')
    if (config['cpus_per_task'], config['threads_per_core'], config['cpu_bind']) != (48, 2, 'cores'):
        raise ValueError('paired_24 requires cpus_per_task=48, threads_per_core=2, cpu_bind=cores')
    if type(config.get('ntasks', 1)) is not int or config.get('ntasks', 1) != 1:
        raise ValueError('paired_24 requires exactly one task')
    return PROFILE

def directives(profile: str | None) -> str:
    return ('#SBATCH --nodes=1\n#SBATCH --cpus-per-task=48\n'
            '#SBATCH --threads-per-core=2\n') if profile else ''

def render_setup(profile: str | None, job_dir: str) -> str:
    if profile is None:
        return ''
    source = Path(__file__).with_name('cpu_topology_probe.py').read_text()
    probe = shlex.quote(job_dir + '/cpu_profile_probe.py')
    directory = shlex.quote(job_dir + '/cpu_profile')
    digest = hashlib.sha256(source.encode()).hexdigest()
    return f'''# Resource opt-in: fail before any runtime/model startup on a wrong cpuset.
_THE_LAB_CPU_DIR={directory}
[ ! -L "$_THE_LAB_CPU_DIR" ] || exit 1
mkdir -p -- "$_THE_LAB_CPU_DIR" || exit $?
chmod 700 "$_THE_LAB_CPU_DIR" || exit $?
cat > {probe} <<'PY_THE_LAB_CPU_PROBE'
{source}PY_THE_LAB_CPU_PROBE
export THE_LAB_CPU_PROFILE=paired_24
export THE_LAB_CPU_PROBE_PATH={probe}
export THE_LAB_CPU_PROBE_SHA256={digest}
export THE_LAB_CPU_RECEIPT_DIR={directory}
export THE_LAB_CPU_PROFILE_GENERATION="${{SLURM_RESTART_COUNT:-0}}"
export THE_LAB_CPU_PROFILE_JOB_ID="${{SLURM_JOB_ID:?CPU profile requires a Slurm job}}"
python3 "$THE_LAB_CPU_PROBE_PATH" --phase batch --job-id "$THE_LAB_CPU_PROFILE_JOB_ID" --restart-count "$THE_LAB_CPU_PROFILE_GENERATION" --output "$THE_LAB_CPU_RECEIPT_DIR/batch.json" || exit $?
'''
