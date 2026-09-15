"""Record a real production-browser gate and immutable before/after inputs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('/Users/jinzhangzheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')


def snapshot():
    paths = set()
    for folder in ('web/src', 'web/public', 'web/tests', 'web/dist-0.9-next', 'tests/fixtures/s57', 'examples'):
        paths.update(p for p in (ROOT / folder).rglob('*') if p.is_file())
    paths.update((ROOT / 'oceanroute').glob('*.py'))
    paths.update(ROOT / name for name in (
        'pyproject.toml', 'web/package.json', 'web/package-lock.json',
        'web/tsconfig.json', 'web/vite.config.ts', 'web/playwright.config.ts',
        'web/index.html', 'scripts/run_development_server.py',
        'docs/USER_MANUAL.md', 'resources/research/s57_sources.json', 'resources/run_0_9_browser_gate.py'))
    return [{'path': p.relative_to(ROOT).as_posix(), 'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(paths)]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def leaves(suites):
    result = []
    for suite in suites:
        for spec in suite.get('specs', []):
            for test in spec.get('tests', []):
                result.append({'title': spec['title'], 'status': test.get('status'),
                    'results': [{'status': r.get('status'), 'retry': r.get('retry'),
                                 'duration': r.get('duration'), 'errors': r.get('errors', [])}
                                for r in test.get('results', [])]})
        result.extend(leaves(suite.get('suites', [])))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stem', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8774')
    parser.add_argument('--test-file')
    parser.add_argument('--artifact-dir')
    args = parser.parse_args()
    if not args.stem.startswith('development_0.9_') or '/' in args.stem:
        parser.error('Use a new development_0.9_ evidence stem')
    base = ROOT / 'resources/validation'
    report = base / (args.stem + '.json')
    execution_path = base / (args.stem + '_execution.json')
    input_path = base / (args.stem + '_inputs.json')
    log = base / (args.stem + '.log')
    if any(p.exists() for p in (report, execution_path, input_path, log)):
        raise RuntimeError('Existing evidence must not be overwritten')
    rows = snapshot()
    write(input_path, {'version': '0.9.0', 'scope':
        'compiled frontend, source, all browser tests, actual backend, S57 fixtures, served manual and configuration',
        'files': rows})
    with urllib.request.urlopen(args.url + '/api/health', timeout=10) as response:
        health = json.load(response)
    assert health['version'] == '0.9.0', health
    served = []
    for path in sorted((ROOT / 'web/dist-0.9-next').rglob('*')):
        if not path.is_file():
            continue
        name = path.relative_to(ROOT / 'web/dist-0.9-next').as_posix()
        with urllib.request.urlopen(args.url + '/' + name, timeout=10) as response:
            data = response.read()
        assert data == path.read_bytes(), name
        served.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    env = os.environ.copy()
    env.update({'OCEANROUTE_E2E_EXTERNAL_SERVER': '1',
                'OCEANROUTE_E2E_BASE_URL': args.url,
                'OCEANROUTE_E2E_API_URL': args.url + '/api',
                'OCEANROUTE_ARTIFACTS_DIR': args.artifact_dir or ('artifacts/dev-0.9/' + args.stem)})
    command = [str(NODE), 'node_modules/@playwright/test/cli.js', 'test', '--reporter=json', '--workers=1', '--retries=0',
               '--output=test-results/' + args.stem]
    if args.test_file:
        command.append(args.test_file)
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT / 'web', env=env,
                               capture_output=True, text=True, timeout=600)
    wall = time.monotonic() - started
    log.write_text(completed.stderr)
    raw = json.loads(completed.stdout)
    write(report, raw)
    results = leaves(raw.get('suites', []))
    after = snapshot()
    changed = sorted({r['path'] for r in rows if r not in after} |
                     {r['path'] for r in after if r not in rows})
    chrome = plistlib.loads(Path('/Applications/Google Chrome.app/Contents/Info.plist').read_bytes())
    actual_pass = completed.returncode == 0 and bool(results) and not changed and not raw.get('errors') and all(
        len(test['results']) == 1 and test['results'][0]['status'] == 'passed'
        and test['results'][0]['retry'] == 0 for test in results) and all(
        raw['stats'].get(key) == 0 for key in ('unexpected', 'skipped', 'flaky'))
    record = {'version': '0.9.0', 'status': 'passed' if actual_pass else 'failed',
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'started_at_utc': started_utc, 'command': command,
        'environment': {key: env[key] for key in ('OCEANROUTE_E2E_EXTERNAL_SERVER',
            'OCEANROUTE_E2E_BASE_URL', 'OCEANROUTE_E2E_API_URL', 'OCEANROUTE_ARTIFACTS_DIR')},
        'chrome_version': chrome['CFBundleShortVersionString'], 'health': health,
        'returncode': completed.returncode, 'wall_time_s': wall, 'stats': raw['stats'],
        'actual_tests': len(results), 'tests': results, 'served_assets': served,
        'input_files': len(rows), 'changed_inputs': changed,
        'input_snapshot_sha256': hashlib.sha256(input_path.read_bytes()).hexdigest()}
    write(execution_path, record)
    print(json.dumps({k: v for k, v in record.items() if k not in ('tests', 'served_assets')}, ensure_ascii=False), flush=True)
    if not actual_pass:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
