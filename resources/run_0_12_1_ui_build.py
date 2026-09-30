"""Compile the actual 0.12 frontend and record the source and output bytes."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('/Users/jinzhangzheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')


def snapshot():
    paths = set()
    for folder in ('web/src', 'web/public'):
        paths.update(p for p in (ROOT / folder).rglob('*') if p.is_file())
    paths.update(ROOT / name for name in ('web/package.json', 'web/package-lock.json',
        'web/tsconfig.json', 'web/vite.config.ts', 'web/index.html', 'docs/USER_MANUAL.md'))
    return [{'path': p.relative_to(ROOT).as_posix(), 'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stem', required=True)
    parser.add_argument('--frontend', default='dist-0.12.1-release')
    args = parser.parse_args()
    if not args.stem.startswith(('development_0.12.1_', 'release_0.12.1_')) or '/' in args.stem:
        parser.error('Use a fresh development_0.12.1_ evidence stem')
    base = ROOT / 'resources/validation'
    report, log = [base / (args.stem + suffix) for suffix in ('.json', '.log')]
    if report.exists() or log.exists():
        raise RuntimeError('Existing build evidence must not be overwritten')
    before = snapshot()
    runs = []
    started = time.monotonic()
    for command in ([str(NODE), 'node_modules/typescript/bin/tsc', '--noEmit'],
                    [str(NODE), 'node_modules/vite/bin/vite.js', 'build', '--outDir', args.frontend]):
        start = time.monotonic()
        result = subprocess.run(command, cwd=ROOT / 'web', capture_output=True, text=True, timeout=180)
        runs.append({'command': command, 'returncode': result.returncode,
                     'wall_time_s': time.monotonic() - start, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode:
            break
    after = snapshot()
    changed = sorted({r['path'] for r in before if r not in after} | {r['path'] for r in after if r not in before})
    assets = [{'path': p.relative_to(ROOT).as_posix(), 'bytes': p.stat().st_size,
               'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
              for p in sorted((ROOT / 'web' / args.frontend).rglob('*')) if p.is_file()]
    passed = len(runs) == 2 and all(r['returncode'] == 0 for r in runs) and not changed and bool(assets)
    record = {'frontend': 'web/' + args.frontend, 'version': '0.12.1', 'status': 'passed' if passed else 'failed',
              'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
              'wall_time_s': time.monotonic() - started, 'runs': runs,
              'inputs': before, 'changed_inputs': changed, 'assets': assets}
    log.write_text('\n'.join(r['stdout'] + r['stderr'] for r in runs))
    report.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in record.items() if k not in ('inputs', 'runs')}, ensure_ascii=False), flush=True)
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
