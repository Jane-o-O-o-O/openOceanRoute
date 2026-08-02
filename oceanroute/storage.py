"""Transactional project snapshots and revision history."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
from typing import Iterator
from uuid import uuid4


class ProjectStore:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            data_dir = Path(os.environ.get("OCEANROUTE_DATA_DIR", Path.cwd() / ".oceanroute"))
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / "projects.sqlite3"
        self.path = str(path)
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL,
                    created_at TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY (project_id, revision)
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def list(self) -> list[dict]:
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                "SELECT id,name,revision,updated_at FROM projects ORDER BY updated_at DESC"
            )]

    def get(self, project_id: str) -> dict:
        with self.connection() as con:
            row = con.execute("SELECT document,revision FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        doc = json.loads(row["document"])
        doc["saved_revision"] = row["revision"]
        return doc

    def save(self, project: dict, *, enforce_revision: bool = True) -> dict:
        doc = json.loads(json.dumps(project, allow_nan=False))
        project_id = str(doc.get("id") or uuid4())
        name = str(doc.get("name") or "未命名工程")
        if len(project_id) > 128 or len(name) > 512:
            raise ValueError("工程标识或名称过长")
        doc["id"] = project_id
        doc["schema_version"] = 1
        expected = doc.pop("saved_revision", None)
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            current = int(previous["revision"]) if previous else 0
            if enforce_revision:
                if expected is None and current:
                    raise ValueError("同ID工程已存在，请打开最新工程继续编辑或另存新ID；缺少修订不能覆盖已有工程")
                if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected != current):
                    raise ValueError("工程已被另一窗口修改或修订号无效，请重新打开最新版本")
            revision = current + 1
            raw = json.dumps(doc, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            con.execute("INSERT INTO projects VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "name=excluded.name,revision=excluded.revision,updated_at=excluded.updated_at,document=excluded.document",
                        (project_id, name, revision, now, raw))
            con.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (project_id, revision, name, now, raw))
        doc["saved_revision"] = revision
        return {"id": project_id, "revision": revision, "updated_at": now, "project": doc}

    def revisions(self, project_id: str) -> list[dict]:
        with self.connection() as con:
            rows = con.execute("SELECT revision,created_at,name FROM revisions WHERE project_id=? ORDER BY revision DESC",
                               (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def restore(self, project_id: str, revision: int) -> dict:
        with self.connection() as con:
            row = con.execute("SELECT document FROM revisions WHERE project_id=? AND revision=?", (project_id, revision)).fetchone()
        if not row:
            raise KeyError((project_id, revision))
        result = self.save(json.loads(row["document"]), enforce_revision=False)
        return result["project"]
