"""Bounded native WAL lifecycle regression, including both persistent stores."""
import json
from pathlib import Path
import subprocess
import sys


def test_concurrent_native_store_lifecycle_and_atomic_revision_restore(tmp_path):
    # A native mutex deadlock cannot be interrupted by Future.result(timeout).
    # Keep this real SQLite regression in a killable child, never a mock/retry.
    code = r'''
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from threading import Barrier
from oceanroute.storage import ProjectStore
from oceanroute.workspace_storage import WorkspaceStore, WorkspaceRevisionConflict

directory = Path(sys.argv[1])
files = [directory / 'shared.sqlite3', directory / 'other.sqlite3']
workspace = {'schema_version': 2, 'id': 'real-wal-workspace', 'name': 'original',
             'currency': 'CNY', 'active_path_id': None, 'paths': [], 'assemblies': [],
             'associations': [], 'cable_types': [{'id': 'declared'}],
             'layers': [], 'terrain_sources': []}
initial = []
for filename in files:
    ProjectStore(filename).save({'id': 'schema1', 'name': 'independent legacy project'})
    initial.append(WorkspaceStore(filename).save(workspace)['workspace'])
start = Barrier(4)
def churn(index):
    start.wait(timeout=10)
    filename = files[index % 2]
    for _ in range(100):
        # Constructors and explicit exits repeatedly open and close real WAL
        # handles in competing threads and across the two store classes/files.
        project = ProjectStore(filename)
        shared = WorkspaceStore(filename)
        with project.connection() as connection:
            assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert connection.execute('SELECT count(*) FROM projects').fetchone()[0] == 1
        with shared.connection() as connection:
            assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            assert connection.execute('SELECT count(*) FROM workspace_projects').fetchone()[0] == 1
    return 100
with ThreadPoolExecutor(max_workers=4) as pool:
    assert sum(pool.map(churn, range(4))) == 400

candidate = deepcopy(initial[0]); candidate['name'] = 'one atomic candidate'
writers = Barrier(2)
def save_candidate(_):
    store = WorkspaceStore(files[0])
    writers.wait(timeout=10)
    try:
        return store.save(candidate)['revision']
    except WorkspaceRevisionConflict:
        return 'conflict'
with ThreadPoolExecutor(max_workers=2) as pool:
    revisions = list(pool.map(save_candidate, range(2)))
assert sorted(map(str, revisions)) == ['2', 'conflict'], revisions
store = WorkspaceStore(files[0])
reopened = store.get(workspace['id'])
assert reopened['name'] == candidate['name'] and reopened['saved_revision'] == 2
restored = store.restore(workspace['id'], 1, expected_revision=2)
assert restored['saved_revision'] == 3
assert {k: v for k, v in restored.items() if k != 'saved_revision'} == {
    k: v for k, v in initial[0].items() if k != 'saved_revision'}
legacy = ProjectStore(files[0])
try:
    with legacy.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("UPDATE projects SET name='should rollback' WHERE id='schema1'")
        raise RuntimeError('forced transaction rollback')
except RuntimeError:
    pass
assert legacy.list()[0]['name'] == 'independent legacy project'
assert WorkspaceStore(files[1]).get(workspace['id']) == initial[1]
print(json.dumps({'native_sqlite': sqlite3.sqlite_version, 'churn_rounds': 400,
                  'managed_wal_connections': 1600, 'revision_outcomes': revisions,
                  'restore_revision': 3, 'rollback_preserved': True}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["churn_rounds"] == 400 and report["managed_wal_connections"] == 1600
    assert sorted(map(str, report["revision_outcomes"])) == ["2", "conflict"]
    assert report["restore_revision"] == 3 and report["rollback_preserved"]
