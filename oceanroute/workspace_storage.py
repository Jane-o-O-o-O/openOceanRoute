"""Transactional schema-2 relationships and full immutable revision snapshots.

Coexists with ProjectStore in the same SQLite file. Current paths, assemblies
and associations are normalized relation tables; revisions retain full JSON.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .workspace import WorkspaceError, validate_workspace
from .sqlite_lifecycle import managed_connection


class WorkspaceRevisionConflict(WorkspaceError):
    def __init__(self, current, expected):
        self.current_revision = current
        self.expected_revision = expected
        super().__init__("WORKSPACE_REVISION_CONFLICT", f"当前修订{current}，请求修订{expected}；请打开最新工作区或另存新ID")


class WorkspaceStore:
    def __init__(self, path=None):
        if path is None:
            directory = Path(os.environ.get("OCEANROUTE_DATA_DIR", Path.cwd()/".oceanroute"))
            directory.mkdir(parents=True, exist_ok=True)
            path = directory/"projects.sqlite3"
        if str(path) == ":memory:":
            raise ValueError("WorkspaceStore使用持久文件或临时文件，不能每次连接独立:memory:")
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_projects (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL,
              revision INTEGER NOT NULL, updated_at TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workspace_paths (
              workspace_id TEXT NOT NULL, path_id TEXT NOT NULL, ordinal INTEGER NOT NULL, document TEXT NOT NULL,
              PRIMARY KEY(workspace_id,path_id), FOREIGN KEY(workspace_id) REFERENCES workspace_projects(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS workspace_assemblies (
              workspace_id TEXT NOT NULL, assembly_id TEXT NOT NULL, ordinal INTEGER NOT NULL, document TEXT NOT NULL,
              PRIMARY KEY(workspace_id,assembly_id), FOREIGN KEY(workspace_id) REFERENCES workspace_projects(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS workspace_associations (
              workspace_id TEXT NOT NULL, id TEXT NOT NULL, path_id TEXT NOT NULL, assembly_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('deployment','alternative')), ordinal INTEGER NOT NULL, document TEXT,
              PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,path_id),
              FOREIGN KEY(workspace_id,path_id) REFERENCES workspace_paths(workspace_id,path_id),
              FOREIGN KEY(workspace_id,assembly_id) REFERENCES workspace_assemblies(workspace_id,assembly_id));
            CREATE UNIQUE INDEX IF NOT EXISTS workspace_one_deployment
              ON workspace_associations(workspace_id,assembly_id) WHERE role='deployment';
            CREATE TABLE IF NOT EXISTS workspace_revisions (
              workspace_id TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL,
              PRIMARY KEY(workspace_id,revision), FOREIGN KEY(workspace_id) REFERENCES workspace_projects(id));
            """)
            if "document" not in {r["name"] for r in con.execute("PRAGMA table_info(workspace_associations)")}:
                con.execute("ALTER TABLE workspace_associations ADD COLUMN document TEXT")

    @contextmanager
    def connection(self):
        with managed_connection(self.path, foreign_keys=True) as con:
            yield con

    @staticmethod
    def _read(con, workspace_id):
        row = con.execute("SELECT metadata,revision FROM workspace_projects WHERE id=?", (workspace_id,)).fetchone()
        if row is None: raise KeyError(workspace_id)
        ws = json.loads(row["metadata"])
        ws["paths"] = [json.loads(r["document"]) for r in con.execute("SELECT document FROM workspace_paths WHERE workspace_id=? ORDER BY ordinal", (workspace_id,))]
        ws["assemblies"] = [json.loads(r["document"]) for r in con.execute("SELECT document FROM workspace_assemblies WHERE workspace_id=? ORDER BY ordinal", (workspace_id,))]
        ws["associations"] = []
        for association_row in con.execute("SELECT id,path_id,assembly_id,role,document FROM workspace_associations WHERE workspace_id=? ORDER BY ordinal", (workspace_id,)):
            link = json.loads(association_row["document"]) if association_row["document"] else {}
            link.update({key: association_row[key] for key in ("id", "path_id", "assembly_id", "role")})
            ws["associations"].append(link)
        ws["saved_revision"] = row["revision"]
        return ws

    def get(self, workspace_id):
        # A read transaction prevents a reader mixing metadata and relationship
        # rows from two revisions while a concurrent writer commits.
        with self.connection() as con:
            con.execute("BEGIN")
            ws = self._read(con, workspace_id)
        return ws

    def list(self):
        with self.connection() as con:
            rows = con.execute("""SELECT w.id,w.name,w.currency,w.revision,w.updated_at,
                (SELECT COUNT(*) FROM workspace_paths p WHERE p.workspace_id=w.id) path_count,
                (SELECT COUNT(*) FROM workspace_assemblies a WHERE a.workspace_id=w.id) assembly_count
                FROM workspace_projects w ORDER BY w.updated_at DESC""").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _expected(value, current):
        if value is None:
            if current: raise WorkspaceRevisionConflict(current, None)
            return
        if isinstance(value, bool) or not isinstance(value, int) or value != current:
            raise WorkspaceRevisionConflict(current, value)

    @staticmethod
    def _write(con, ws, revision, now):
        ws = dict(ws); ws.pop("saved_revision", None)
        raw = json.dumps(ws, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        metadata = {k: v for k, v in ws.items() if k not in {"paths", "assemblies", "associations"}}
        con.execute("""INSERT INTO workspace_projects VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,currency=excluded.currency,revision=excluded.revision,updated_at=excluded.updated_at,metadata=excluded.metadata""",
            (ws["id"], ws["name"], ws["currency"], revision, now, json.dumps(metadata, ensure_ascii=False, allow_nan=False)))
        for table in ("workspace_associations", "workspace_paths", "workspace_assemblies"):
            con.execute(f"DELETE FROM {table} WHERE workspace_id=?", (ws["id"],))
        for i, path in enumerate(ws["paths"]):
            con.execute("INSERT INTO workspace_paths VALUES(?,?,?,?)", (ws["id"], path["id"], i, json.dumps(path, ensure_ascii=False, allow_nan=False)))
        for i, assembly in enumerate(ws["assemblies"]):
            con.execute("INSERT INTO workspace_assemblies VALUES(?,?,?,?)", (ws["id"], assembly["id"], i, json.dumps(assembly, ensure_ascii=False, allow_nan=False)))
        for i, link in enumerate(ws["associations"]):
            con.execute("INSERT INTO workspace_associations(workspace_id,id,path_id,assembly_id,role,ordinal,document) VALUES(?,?,?,?,?,?,?)",
                        (ws["id"], link["id"], link["path_id"], link["assembly_id"], link["role"], i, json.dumps(link, ensure_ascii=False, allow_nan=False)))
        con.execute("INSERT INTO workspace_revisions VALUES(?,?,?,?,?)", (ws["id"], revision, ws["name"], now, raw))
        ws["saved_revision"] = revision
        return {"id": ws["id"], "revision": revision, "updated_at": now, "workspace": ws}

    def save(self, workspace, *, expected_revision=None):
        ws = validate_workspace(workspace)
        supplied = ws.pop("saved_revision", None)
        expected = supplied if expected_revision is None else expected_revision
        if expected_revision is not None and supplied is not None and expected_revision != supplied:
            raise WorkspaceRevisionConflict(supplied, expected_revision)
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT revision FROM workspace_projects WHERE id=?", (ws["id"],)).fetchone()
            current = row["revision"] if row else 0
            self._expected(expected, current)
            return self._write(con, ws, current+1, now)

    def revisions(self, workspace_id):
        with self.connection() as con:
            if not con.execute("SELECT 1 FROM workspace_projects WHERE id=?", (workspace_id,)).fetchone(): raise KeyError(workspace_id)
            rows = con.execute("SELECT revision,name,created_at FROM workspace_revisions WHERE workspace_id=? ORDER BY revision DESC", (workspace_id,)).fetchall()
        return [dict(r) for r in rows]

    def restore(self, workspace_id, revision, *, expected_revision):
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("恢复revision须为正整数")
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT revision FROM workspace_projects WHERE id=?", (workspace_id,)).fetchone()
            if current is None: raise KeyError(workspace_id)
            self._expected(expected_revision, current["revision"])
            row = con.execute("SELECT document FROM workspace_revisions WHERE workspace_id=? AND revision=?", (workspace_id, revision)).fetchone()
            if row is None: raise KeyError((workspace_id, revision))
            ws = validate_workspace(json.loads(row["document"]))
            return self._write(con, ws, current["revision"]+1, now)["workspace"]
