"""Create an explicitly reconstructed history without changing product files."""
from pathlib import Path
from datetime import datetime, timezone
import collections
import hashlib
import json
import os
import re
import subprocess
import zipfile

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[2] if SCRIPT.parent.name == 'commit-import-audit' else SCRIPT.parents[1]
AUDIT = ROOT / '.git/commit-import-audit'
COUNTS = [28, 26, 24, 28, 22, 26, 22, 24, 22, 28, 22, 20, 8]
LABELS = [f'0.{i}' for i in range(1, 13)] + ['0.12.1']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args, input=None, env=None, check=True):
    return subprocess.run(['git', *args], cwd=ROOT, input=input,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env, check=check)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def selected(name):
    if not name.startswith('OceanRoute/'):
        return False
    name = name.removeprefix('OceanRoute/')
    p = Path(name)
    if '__pycache__' in p.parts or p.suffix == '.pyc':
        return False
    if name in {'README.md', 'pyproject.toml', 'launcher.py', 'web/package.json',
                'web/package-lock.json', 'web/index.html', 'web/tsconfig.json',
                'web/vite.config.ts', 'web/playwright.config.ts', 'web/.gitignore'}:
        return True
    if name.startswith('oceanroute/'):
        return p.suffix in {'.py', '.md'}
    if name.startswith('docs/'):
        return p.suffix == '.md'
    if name.startswith(('examples/', 'tests/fixtures/')):
        return True
    if name.startswith('tests/'):
        return p.suffix == '.py'
    if name.startswith('scripts/'):
        return p.suffix in {'.py', '.sh', '.bat'}
    if name.startswith(('web/src/', 'web/tests/', 'web/public/')):
        return p.suffix not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
    if name.startswith('resources/research/'):
        return p.name in {'manual_findings.md', 'website_findings.md',
                          'web_sources.json', 's57_sources.json', 'arc_sources.json'}
    if p.parent.as_posix() == 'resources':
        return p.suffix == '.py'
    if p.parent.as_posix() == 'packaging/windows':
        return p.suffix in {'.py', '.nsi', '.nsh', '.txt'}
    return False


def load_snapshots():
    snapshots, archives = {}, []
    suspicious = re.compile(rb'(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-proj-[A-Za-z0-9_-]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
    for label in LABELS:
        path = ROOT / 'outputs/releases' / f'OceanRoute-{label}-portable.zip'
        original = path.read_bytes()
        with zipfile.ZipFile(path) as z:
            require(z.testzip() is None, 'Invalid CRC: ' + label)
            require(len(z.namelist()) == len(set(z.namelist())), 'Duplicate ZIP paths')
            files = {}
            for info in z.infolist():
                name = info.orig_filename
                require(name == info.filename and '\\' not in name and '\0' not in name
                        and not name.startswith('/') and '..' not in Path(name).parts,
                        'Unsafe source archive path')
                if selected(name):
                    data = z.read(info)
                    require(len(data) < 100 * 1024 * 1024, 'Oversize Git file: ' + name)
                    require(not suspicious.search(data), 'Possible credential in ' + name)
                    files[name.removeprefix('OceanRoute/')] = data
        snapshots[label] = files
        archives.append({'stage': label, 'path': path.relative_to(ROOT).as_posix(),
                         'bytes': len(original), 'sha256': sha(original)})
    return snapshots, archives


def write_new(path, data):
    require(not path.exists(), 'Refusing to replace existing history metadata: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def main():
    require(git('rev-parse', '--verify', 'HEAD', check=False).returncode != 0,
            'Existing history must not be replaced')
    require(not git('ls-files', '-z').stdout, 'Existing tracked files must not be replaced')
    require(not git('for-each-ref', '--format=%(refname)', 'refs/heads/', 'refs/tags/',
                    'refs/remotes/').stdout.strip(), 'Existing user history refs detected')
    draft = json.loads((AUDIT / 'commit-plan-draft.json').read_text())
    plan = draft['commits'] if isinstance(draft, dict) else draft
    require(len(plan) == 300, 'Plan must contain exactly 300 commits')
    dates = json.loads((AUDIT / 'reconstructed-timestamps.json').read_text())['timestamps']
    require(len(dates) == 300 and dates == sorted(dates) and len(set(dates)) == 300,
            'Invalid timestamp schedule')
    snapshots, archives = load_snapshots()
    previous = {}
    for label, count in zip(LABELS, COUNTS):
        groups = [p for p in plan if p['stage'] == label]
        require(len(groups) == count, 'Stage allocation mismatch: ' + label)
        current = snapshots[label]
        changes = {p for p in previous.keys() | current.keys() if previous.get(p) != current.get(p)}
        grouped = [p for group in groups for p in group['files']]
        require(all(group['files'] for group in groups) and len(grouped) == len(set(grouped))
                and set(grouped) == changes, 'Incomplete/duplicate/empty stage diff: ' + label)
        previous = current
    require([p['stage'] for p in plan] == [label for label, count in zip(LABELS, COUNTS) for _ in range(count)],
            'Version order must be preserved')
    final_source = snapshots[LABELS[-1]]
    for path, data in final_source.items():
        require((ROOT / path).is_file() and not (ROOT / path).is_symlink()
                and (ROOT / path).read_bytes() == data, 'Product source changed: ' + path)
    for i, item in enumerate(plan):
        require(item['type'] in {'chore', 'feat', 'fix', 'test', 'docs', 'build'}, 'Unknown commit type')
        require('\n' not in item['subject'] and item['subject'].strip(), 'Invalid subject')
        item.update(number=i + 1, author_date=dates[i], committer_date=dates[i],
                    reconstruction=True)
    for path in ['.gitignore', 'HISTORY_RECONSTRUCTION.md', 'history/commit-plan.json', 'history/reconstruct_history.py']:
        require(path not in plan[-1]['files'], 'Duplicate metadata path')
        plan[-1]['files'].append(path)
    plan[-1]['subject'] += ' and record the reconstructed chronology'
    summary = json.loads((AUDIT / 'github-2026-august-september.json').read_text())
    declaration = '''# Reconstructed Git history / 重建 Git 历史

This repository history was reconstructed on 2026-10-07 from the actual frozen
OceanRoute 0.1 through 0.12.1 source-release snapshots. These are 300 nonempty
changes grouped by software domain, tests, interface, documentation, and build
tools. They are not the original development commits and are not evidence that
this project was developed or originally committed during August or September.

本仓库的300个提交是在2026-10-07，根据已封存的0.1至0.12.1真实源码快照重建。
为满足仓库所有者的明确要求，Author和Committer日期统一安排在2026年八月及九月：
八月140个、九月160个。这些日期是重建元数据，不是当时真实开发或提交的时间。
每个提交说明都带有Reconstructed-History与实际生成日期标记。

## Date allocation

The authenticated GitHub commit search returned 206 indexed, visible commits
for the account author in August/September 2026: 96 in August and 110 in September.
Converted to Asia/Shanghai, their mean times of day were 17:19:48 and 19:39:36,
respectively. This is a scoped search result, not a claim of completeness for
every private or unindexed branch. Other repository names and raw activity are
kept in the local `.git/commit-import-audit/` directory and are not published.

The monthly ratio is scaled to 140/160. Counts use largest-remainder allocation
over the observed 59 active dates; times use empirical within-day quantiles.
Duplicate times are separated by one second. Reconstructed mean times are
17:25:30 in August and 19:42:11 in September; no exact average-match is claimed.
Both Git timestamps use the same +08:00 scheduled date. `history/commit-plan.json`
lists all 300 groups, their paths and dates, and the original snapshot hashes.

## Preservation and scope

The final tracked product source bytes equal the frozen 0.12.1 snapshot and the
existing workspace. Intermediate groups are review units; only completed release
boundaries represent the corresponding complete selected source snapshot.
The history does not claim each intermediate group is independently runnable.
Tagged boundaries use `reconstructed/v...` to distinguish them from original tags.

No production feature or released EXE/wheel/PDF/source archive was changed.
Runtime caches, installed dependencies, Wine prefixes, temporary QA copies,
application databases, original vendor manuals, and generated builds/reports
are excluded. Small existing scientific test fixtures are retained with their
existing source notices so regression tests can use their actual input data.
Windows machine acceptance remains unperformed; recorded Wine test results are
not native Windows machine acceptance. Existing engineering/model limitations
remain documented in the source manuals and implementation status.

The import script reads local frozen release ZIPs, never checks out partial
snapshots into the product workspace, and refuses to replace existing Git history.
It creates dated commit objects and verifies source parity before attaching the
new `main` ref. No existing remote history is overwritten or force-pushed.
'''
    ignore = (ROOT / '.gitignore').read_bytes() + b'''\n# Local runtime/build/private QA data; tracked source is explicitly selected.\n.oceanroute*/\nweb/dist*/\nweb/artifacts/\noutput/\noutputs/\nreleases/\nresources/*pdf_qa*/\nresources/product_pdf*/\nresources/development_pdf*/\nresources/frozen_ui*/\nresources/installation_candidates*/\nresources/validation/\nresources/research/*\n!resources/research/manual_findings.md\n!resources/research/website_findings.md\n!resources/research/web_sources.json\n!resources/research/s57_sources.json\n!resources/research/arc_sources.json\npackaging/windows/*/\npackaging/windows/*child.json\npackaging/windows/windows-backend-child.json\n!oceanroute/manual.md\n'''
    (ROOT / '.gitignore').write_bytes(ignore)
    write_new(ROOT / 'HISTORY_RECONSTRUCTION.md', declaration.encode())
    write_new(ROOT / 'history/reconstruct_history.py', Path(__file__).read_bytes())
    plan_record = {'schema_version': 1, 'reconstructed_history': True,
                   'actually_generated_on': '2026-10-07', 'timezone': 'Asia/Shanghai',
                   'author_and_committer_dates_intentionally_reconstructed': True,
                   'stage_counts': dict(zip(LABELS, COUNTS)), 'original_source_archives': archives,
                   'observed_activity_summary': {k: summary[k] for k in ['total', 'august', 'september']},
                   'commits': plan}
    write_new(ROOT / 'history/commit-plan.json', (json.dumps(plan_record, ensure_ascii=False, indent=2) + '\n').encode())
    extras = {p: (ROOT / p).read_bytes() for p in ['.gitignore', 'HISTORY_RECONSTRUCTION.md',
              'history/commit-plan.json', 'history/reconstruct_history.py']}
    final_target = {**final_source, **extras}
    index = AUDIT / 'reconstruction.index'
    require(not index.exists(), 'Existing reconstruction index detected')
    env = {**os.environ, 'GIT_INDEX_FILE': str(index)}
    git('read-tree', '--empty', env=env)
    author = git('config', '--get', 'user.name').stdout.decode().strip()
    email = git('config', '--get', 'user.email').stdout.decode().strip()
    require(author and email, 'Missing configured author identity')
    parent = None
    previous_tree = git('write-tree', env=env).stdout.decode().strip()
    ledger, checkpoints, object_cache = [], [], {}
    for item in plan:
        current = snapshots[item['stage']]
        updates = []
        for path in item['files']:
            data = extras.get(path, current.get(path))
            if data is None:
                updates.append(f'0 {"0" * 40}\t{path}\0'.encode())
                continue
            key = sha(data)
            if key not in object_cache:
                object_cache[key] = git('hash-object', '-w', '--stdin', input=data).stdout.decode().strip()
            mode = '100755' if (ROOT / path).is_file() and (ROOT / path).stat().st_mode & 0o111 else '100644'
            updates.append(f'{mode} {object_cache[key]}\t{path}\0'.encode())
        git('update-index', '-z', '--index-info', input=b''.join(updates), env=env)
        tree = git('write-tree', env=env).stdout.decode().strip()
        require(tree != previous_tree, 'Empty commit is forbidden')
        message = f"{item['type']}({item['scope']}): {item['subject']}\n\nReconstructed-History: true\nActually-Generated-On: 2026-10-07\nOriginal-Release-Snapshot: {item['stage']}\nReconstructed-Group: {item['number']}/300\nThe author and committer dates were explicitly requested by the repository owner.\nThese are reconstructed source snapshot changes, not original development commits.\n"
        commit_env = {**env, 'GIT_AUTHOR_NAME': author, 'GIT_AUTHOR_EMAIL': email,
                      'GIT_COMMITTER_NAME': author, 'GIT_COMMITTER_EMAIL': email,
                      'GIT_AUTHOR_DATE': item['author_date'], 'GIT_COMMITTER_DATE': item['committer_date']}
        args = ['commit-tree', tree] + (['-p', parent] if parent else [])
        commit = git(*args, input=message.encode(), env=commit_env).stdout.decode().strip()
        ledger.append({**item, 'commit': commit, 'tree': tree})
        parent, previous_tree = commit, tree
        if item['number'] == 300 or plan[item['number']]['stage'] != item['stage']:
            expected = snapshots[item['stage']]
            if item['number'] == 300:
                expected = final_target
            names = git('ls-tree', '-r', '--name-only', commit).stdout.decode().splitlines()
            require(set(names) == set(expected), 'Checkpoint path set differs: ' + item['stage'])
            for path, data in expected.items():
                require(git('show', commit + ':' + path).stdout == data, 'Checkpoint bytes differ: ' + path)
            checkpoints.append({'stage': item['stage'], 'commit': commit, 'selected_source_files': len(expected), 'all_byte_identical': True})
            print(json.dumps({'completed': item['number'], 'stage': item['stage'], 'commit': commit}), flush=True)
        with (AUDIT / 'reconstructed-commits.jsonl').open('a') as stream:
            stream.write(json.dumps(ledger[-1], ensure_ascii=False) + '\n')
    require(int(git('rev-list', '--count', parent).stdout) == 300, 'Wrong total commit count')
    for path, data in final_target.items():
        require((ROOT / path).read_bytes() == data, 'Worktree source changed during reconstruction: ' + path)
    for archive in archives:
        require(sha((ROOT / archive['path']).read_bytes()) == archive['sha256'], 'Frozen archive changed')
    git('update-ref', 'refs/heads/main', parent, '')
    git('symbolic-ref', 'HEAD', 'refs/heads/main')
    git('read-tree', 'main')
    for checkpoint in checkpoints:
        version = checkpoint['stage'] if checkpoint['stage'] == '0.12.1' else checkpoint['stage'] + '.0'
        git('tag', 'reconstructed/v' + version, checkpoint['commit'])
    require(not git('diff', '--name-only', 'HEAD').stdout, 'Final tracked worktree differs')
    result = {'status': 'passed', 'reconstruction': True, 'actually_generated_on': '2026-10-07',
              'head': parent, 'commits': 300, 'final_files': len(final_target),
              'monthly_counts': {'2026-08': 140, '2026-09': 160},
              'type_counts': dict(collections.Counter(p['type'] for p in plan)),
              'source_archives': archives, 'checkpoints': checkpoints, 'commit_records': ledger,
              'remote_pushed': False}
    write_new(AUDIT / 'local-reconstruction-execution.json', (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode())
    print(json.dumps({k: v for k, v in result.items() if k not in ['source_archives', 'checkpoints', 'commit_records']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
