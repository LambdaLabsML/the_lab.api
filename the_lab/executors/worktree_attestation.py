"""Standalone, stdlib-only checkout attestation staged outside the solver tree.

Checks Git's reciprocal administrative identity and hashes tracked bytes without
trusting the index (including assume-unchanged/skip-worktree bits). Untracked
runtime assets are intentionally outside this source contract.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


def git(repo: Path, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                   stderr=subprocess.DEVNULL)


def attest(bare: str, worktree: str, expected: str) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected):
        raise ValueError('expected commit must be a full object ID')
    # Bind the requested allocation path itself, not a different checkout
    # reached through a symlink at the root or any parent directory.
    if any(not Path(raw).is_absolute() or '..' in Path(raw).parts for raw in (worktree, bare)):
        raise ValueError('noncanonical requested path')
    root, common = Path(worktree), Path(bare)
    if root.resolve(strict=True) != root or common.resolve(strict=True) != common:
        raise ValueError('worktree/common repository path alias')
    root_identity = root.stat()
    gitfile = root / '.git'
    if gitfile.is_symlink() or not gitfile.is_file():
        raise ValueError('missing regular worktree gitfile')
    pointer = gitfile.read_text().strip()
    if not pointer.startswith('gitdir: '):
        raise ValueError('invalid worktree gitfile')
    raw_admin = Path(pointer[8:])
    if not raw_admin.is_absolute() or '..' in raw_admin.parts:
        raise ValueError('noncanonical administrative path')
    admin = raw_admin.resolve(strict=True)
    if admin != raw_admin or admin.parent != common / 'worktrees':
        raise ValueError('unexpected administrative directory')
    reverse = Path((admin / 'gitdir').read_text().strip())
    if reverse != gitfile or reverse.resolve(strict=True) != gitfile:
        raise ValueError('nonreciprocal administrative gitdir')
    if (admin / (admin / 'commondir').read_text().strip()).resolve(strict=True) != common:
        raise ValueError('wrong common repository')
    if not (admin / 'locked').is_file():
        raise ValueError('worktree registration is not persistently locked')
    if (admin / 'HEAD').read_text().strip() != expected:
        raise ValueError('detached administrative HEAD differs from expected commit')
    if git(root, 'rev-parse', 'HEAD').decode().strip() != expected:
        raise ValueError('resolved HEAD differs from expected commit')
    if git(common, 'rev-parse', '--is-bare-repository').strip() != b'true':
        raise ValueError('common repository is not bare')
    object_format = git(common, 'rev-parse', '--show-object-format').decode().strip()
    if object_format not in ('sha1', 'sha256'):
        raise ValueError('unsupported object format')
    tree = git(common, 'ls-tree', '-rz', '--full-tree', expected)
    digest = hashlib.sha256()
    count = 0
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        header, raw_path = entry.split(b'\t', 1)
        mode, kind, oid = header.split()
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid tracked path')
        path = root / relative
        # Never traverse a substituted parent symlink.
        if path.parent.resolve(strict=True) != root.joinpath(*relative.parts[:-1]):
            raise ValueError('tracked path parent changed: ' + str(relative))
        st = path.lstat()
        if mode == b'120000' and stat.S_ISLNK(st.st_mode):
            data = os.fsencode(os.readlink(path))
        elif mode in (b'100644', b'100755') and stat.S_ISREG(st.st_mode):
            if bool(st.st_mode & 0o111) != (mode == b'100755'):
                raise ValueError('tracked executable mode changed: ' + str(relative))
            data = path.read_bytes()
        else:
            raise ValueError('unsupported or changed tracked type: ' + str(relative))
        if kind != b'blob':
            raise ValueError('unsupported non-blob tracked entry')
        actual = hashlib.new(object_format, b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual.encode() != oid:
            raise ValueError('tracked bytes differ: ' + str(relative))
        after = path.lstat()
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('tracked file changed during attestation')
        digest.update(entry + b'\0')
        count += 1
    # Recheck administrative identity after reading the tree.
    end_root = root.lstat()
    if root.resolve(strict=True) != root or not stat.S_ISDIR(end_root.st_mode) or (root_identity.st_dev, root_identity.st_ino) != (end_root.st_dev, end_root.st_ino):
        raise ValueError('allocation directory changed during attestation')
    if gitfile.read_text().strip() != pointer or (admin / 'gitdir').read_text().strip() != str(gitfile) or (admin / 'HEAD').read_text().strip() != expected:
        raise ValueError('administrative identity changed during attestation')
    return {'schema': 1, 'qualified': True, 'expected_commit': expected,
            'worktree': str(root), 'gitdir': str(admin), 'common_dir': str(common),
            'tracked_files': count, 'tracked_tree_entries_sha256': digest.hexdigest(),
            'observed_at_ns': time.time_ns(), 'untracked_runtime_files_checked': False}


def main() -> int:
    bare, worktree, expected, receipt = sys.argv[1:]
    try:
        result = attest(bare, worktree, expected)
    except Exception as exc:
        result = {'schema': 1, 'qualified': False, 'expected_commit': expected,
                  'worktree': worktree, 'error': str(exc), 'observed_at_ns': time.time_ns()}
    # Unique per-phase/generation receipt; never overwrite prior failure evidence.
    with open(receipt, 'x', encoding='utf-8') as out:
        json.dump(result, out, sort_keys=True)
        out.write('\n')
    if not result['qualified']:
        print('the-lab: source attestation failed: ' + result['error'], file=sys.stderr)
        return 125
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
