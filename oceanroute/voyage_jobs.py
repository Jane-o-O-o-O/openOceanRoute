"""Local, bounded background jobs with atomic durable completed-state files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from uuid import UUID, uuid4

from .voyage import _finite_json, read_voyage_checkpoint, run_voyage


def _now():
    return datetime.now(timezone.utc).isoformat()


class VoyageJobs:
    def __init__(self, directory, *, lazy=False, max_storage_bytes=1_000_000_000, max_saved_jobs=250):
        self.directory = Path(directory).resolve()
        self.lock = threading.RLock()
        self.executor = None
        self.cancellations = {}
        self.closed = False
        self.started = False
        self.owner_file = None
        self.max_storage_bytes = int(max_storage_bytes)
        self.max_saved_jobs = int(max_saved_jobs)
        if self.max_storage_bytes < 10000 or not 1 <= self.max_saved_jobs <= 10000:
            raise ValueError("invalid local job storage quota")
        if not lazy:
            self.start()

    def start(self):
        with self.lock:
            if self.closed:
                raise ValueError("voyage job manager is closed")
            if self.started:
                return
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            lease = self.directory/".owner.lock"
            if lease.is_symlink():
                raise ValueError("job ownership lock cannot be a symlink")
            fd = os.open(lease, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            owner = os.fdopen(fd, "r+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        owner.write(b"0"); owner.flush()
                    owner.seek(0); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                owner.close()
                raise ValueError("Another OceanRoute process already owns this voyage job directory; use its server or a different OCEANROUTE_DATA_DIR") from error
            self.owner_file = owner
            try:
                self._recover()
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oceanroute-voyage")
                self.started = True
            except Exception:
                owner.close(); self.owner_file = None
                raise

    def _ensure_started(self, *, read_only=False):
        if self.closed and read_only:
            return
        self.start()

    def _folders(self):
        if not self.directory.exists():
            return []
        return [p for p in self.directory.iterdir() if not p.is_symlink() and p.is_dir()
                and self._canonical_id(p.name)]

    @staticmethod
    def _canonical_id(identifier):
        try:
            return isinstance(identifier, str) and str(UUID(identifier)) == identifier
        except (ValueError, AttributeError):
            return False

    def _recover(self):
        # Restart cannot reconstruct an interrupted partial numerical step.
        # Completed chunk checkpoints remain available for an explicit resume.
        for folder in self._folders():
            path = folder/"status.json"
            try:
                status = self._read(path)
                if (not isinstance(status, dict) or not isinstance(status.get("status"), str) or status.get("status") not in
                        {"queued", "running", "cancelling", "interrupted", "failed", "completed", "stopped", "cancelled"}
                        or not isinstance(status.get("created_at"), str) or not status["created_at"]
                        or (status.get("progress") is not None and not isinstance(status["progress"], dict))):
                    raise ValueError("job status has invalid required metadata")
                _finite_json(status, 64_000)
            except (ValueError, KeyError, OSError) as error:
                if path.exists() or path.is_symlink():
                    os.replace(path, folder/("status.corrupt-"+str(uuid4())+".json"))
                status = {"id": folder.name, "status": "failed", "created_at": _now(),
                          "detail": "Corrupt job status was isolated: "+str(error)}
            allowed = {"id", "status", "created_at", "updated_at", "parent_job_id", "completed_chunks",
                       "checkpoint_available", "result_available", "progress", "summary", "detail", "stop_reason"}
            status = {key: value for key, value in status.items() if key in allowed}
            status["id"] = folder.name
            checkpoint = None
            try:
                checkpoint = read_voyage_checkpoint(self._read(folder/"checkpoint.json"))
            except (ValueError, KeyError, OSError):
                pass
            status["checkpoint_available"] = checkpoint is not None
            if checkpoint:
                physical = checkpoint["physical_checkpoint"]
                old_progress = status.get("progress") or {}
                status.update(completed_chunks=checkpoint["completed_chunks"],
                              progress={**old_progress, "time_s": physical["time_s"],
                                        "nodes": len(physical["state"]["positions"]),
                                        "paid_out_m": physical["state"]["paid_out_m"],
                                        "accepted_mesh_records": len(checkpoint["mesh_history"])})
            status["result_available"] = False
            try:
                result = self._read(folder/"result.json")
                if (not isinstance(result, dict) or not isinstance(result.get("status"), str)
                        or result.get("status") not in {"completed", "stopped", "cancelled"}
                        or not isinstance(result.get("summary"), dict)):
                    raise ValueError("invalid completed result")
                _finite_json(result["summary"], 32_000)
                if result.get("checkpoint") is not None:
                    recovered = read_voyage_checkpoint(result["checkpoint"])
                    if checkpoint is None or recovered["physical_checkpoint"]["time_s"] >= checkpoint["physical_checkpoint"]["time_s"]:
                        checkpoint = recovered
                        # A completed result also embeds this exact state. No
                        # redundant data write is needed during quota recovery.
                        status["checkpoint_available"] = True
                        status["completed_chunks"] = checkpoint["completed_chunks"]
                status.update(status=result["status"], result_available=True,
                              stop_reason=result.get("stop_reason"), summary=result["summary"])
            except (ValueError, KeyError, OSError):
                pass
            if status.get("status") in {"queued", "running", "cancelling"}:
                status.update(status="interrupted", updated_at=_now(),
                              detail="Application stopped; resume from the latest completed chunk checkpoint")
            if checkpoint:
                physical = checkpoint["physical_checkpoint"]
                status["progress"] = {**(status.get("progress") or {}), "time_s": physical["time_s"],
                                      "nodes": len(physical["state"]["positions"]), "paid_out_m": physical["state"]["paid_out_m"],
                                      "accepted_mesh_records": len(checkpoint["mesh_history"])}
            self._store_write(path, status)

    @staticmethod
    def _read(path):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("job files and directories cannot be symlinks")
        if not path.exists():
            raise KeyError(str(path))
        if path.stat().st_size > 64_000_000:
            raise ValueError("job file exceeds 64 MB")
        value = json.loads(path.read_text(encoding="utf-8"))
        _finite_json(value, 64_000_000)
        return value

    @staticmethod
    def _write(path, value, limit=64_000_000):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("job files and directories cannot be symlinks")
        data = _finite_json(value, limit)
        fd, name = tempfile.mkstemp(prefix="."+path.name+"-", suffix=".tmp", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)

    def _store_write(self, path, value, limit=64_000_000):
        if path.name == "status.json":
            # Recovery/read/delete must remain available when an administrator
            # lowers a quota below already saved data; metadata is bounded.
            self._write(path, value, min(limit, 64_000))
            return
        data = _finite_json(value, limit)
        usage = sum(p.stat().st_size for folder in self._folders() for p in folder.iterdir()
                    if not p.is_symlink() and p.is_file() and p != path)
        reserve = min(1_000_000, self.max_storage_bytes//10)
        quota = self.max_storage_bytes if path.name == "status.json" else self.max_storage_bytes-reserve
        if usage+len(data) > quota:
            raise ValueError("local voyage storage quota exceeded; delete an inactive job before continuing")
        self._write(path, value, limit)

    def _folder(self, identifier):
        if not self._canonical_id(identifier):
            raise ValueError("job id must be a canonical UUID")
        path = self.directory/identifier
        if path.is_symlink():
            raise ValueError("job directory cannot be a symlink")
        if not path.is_dir():
            raise KeyError(identifier)
        return path

    def get(self, identifier):
        self._ensure_started(read_only=True)
        with self.lock:
            return self._read(self._folder(identifier)/"status.json")

    def list(self):
        self._ensure_started(read_only=True)
        with self.lock:
            rows = [self._read(p/"status.json") for p in self._folders()]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    def submit(self, project, config, *, parent_job_id=None):
        self._ensure_started()
        if not isinstance(project, dict) or not isinstance(config, dict):
            raise ValueError("job project/config must be objects")
        _finite_json({"project": project, "config": config}, 32_000_000)
        with self.lock:
            if self.closed:
                raise ValueError("voyage job manager is closed")
            if len(self._folders()) >= self.max_saved_jobs:
                raise ValueError("local saved job capacity reached; delete an inactive job")
            active = sum(r["status"] in {"queued", "running", "cancelling"} for r in self.list())
            if active >= 4:
                raise ValueError("At most four local voyage jobs may be running or queued")
            identifier = str(uuid4()); folder = self.directory/identifier; folder.mkdir(mode=0o700)
            request = {"project": deepcopy(project), "config": deepcopy(config), "parent_job_id": parent_job_id}
            status = {"id": identifier, "status": "queued", "created_at": _now(), "updated_at": _now(),
                      "parent_job_id": parent_job_id, "completed_chunks": 0, "checkpoint_available": False,
                      "result_available": False, "progress": None}
            try:
                self._store_write(folder/"request.json", request, 32_000_000)
                self._store_write(folder/"status.json", status)
            except Exception:
                shutil.rmtree(folder)
                raise
            event = threading.Event(); self.cancellations[identifier] = event
            self.executor.submit(self._run, identifier, event)
            return status

    def _run(self, identifier, event):
        folder = self.directory/identifier
        try:
            with self.lock:
                status = self._read(folder/"status.json")
                status.update(status="running", updated_at=_now()); self._store_write(folder/"status.json", status)
            request = self._read(folder/"request.json")

            def completed(value):
                with self.lock:
                    self._store_write(folder/"checkpoint.json", value["checkpoint"], 16_000_000)
                    self._store_write(folder/"latest_chunk.json", value["chunk"])
                    status = self._read(folder/"status.json")
                    checkpoint = value["checkpoint"]
                    physical = checkpoint["physical_checkpoint"]
                    status.update(status="cancelling" if event.is_set() else "running", updated_at=_now(),
                                  checkpoint_available=True, completed_chunks=checkpoint["completed_chunks"],
                                  progress={"time_s": physical["time_s"], "requested_end_time_s": value["requested_end_time_s"],
                                            "nodes": len(physical["state"]["positions"]),
                                            "paid_out_m": physical["state"]["paid_out_m"],
                                            "accepted_mesh_records": len(checkpoint["mesh_history"]),
                                            "elapsed_wall_s": value["elapsed_wall_s"]})
                    self._store_write(folder/"status.json", status)

            result = run_voyage(request["project"], request["config"], on_chunk=completed, should_cancel=event.is_set)
            with self.lock:
                self._store_write(folder/"result.json", result)
                if result["checkpoint"] is not None:
                    self._store_write(folder/"checkpoint.json", result["checkpoint"], 16_000_000)
                status = self._read(folder/"status.json")
                status.update(status=result["status"], stop_reason=result["stop_reason"], updated_at=_now(),
                              checkpoint_available=result["checkpoint"] is not None, result_available=True,
                              summary=result["summary"])
                self._store_write(folder/"status.json", status)
        except Exception as error:
            with self.lock:
                status = self._read(folder/"status.json")
                status.update(status="failed", updated_at=_now(), detail=str(error),
                              checkpoint_available=(folder/"checkpoint.json").exists())
                self._store_write(folder/"status.json", status)
        finally:
            with self.lock:
                self.cancellations.pop(identifier, None)

    def cancel(self, identifier):
        self._ensure_started()
        with self.lock:
            status = self.get(identifier)
            if identifier in self.cancellations and status["status"] in {"queued", "running", "cancelling"}:
                self.cancellations[identifier].set()
                status.update(status="cancelling", updated_at=_now())
                self._store_write(self._folder(identifier)/"status.json", status)
            return status

    def checkpoint(self, identifier):
        self._ensure_started(read_only=True)
        with self.lock:
            folder = self._folder(identifier)
            try:
                return read_voyage_checkpoint(self._read(folder/"checkpoint.json"))
            except (ValueError, KeyError):
                result = self._read(folder/"result.json")
                if not isinstance(result, dict) or result.get("checkpoint") is None:
                    raise KeyError(identifier)
                return read_voyage_checkpoint(result["checkpoint"])

    def result(self, identifier):
        self._ensure_started(read_only=True)
        with self.lock:
            return self._read(self._folder(identifier)/"result.json")

    def resume(self, identifier, options):
        self._ensure_started()
        if not isinstance(options, dict):
            raise ValueError("resume options must be an object")
        allowed = {"duration_s", "chunk_duration_s", "max_chunks", "max_output_frames", "max_total_work_units", "max_mesh_records"}
        if set(options)-allowed:
            raise ValueError("resume restores actual physics; options may only change duration/output/work limits")
        with self.lock:
            if self.get(identifier)["status"] in {"queued", "running", "cancelling"}:
                raise ValueError("Finish or cancel the active job before resuming its saved state")
            checkpoint = self.checkpoint(identifier)
            request = self._read(self._folder(identifier)/"request.json")
            return self.submit(request["project"], {**options, "resume_state": checkpoint}, parent_job_id=identifier)

    def delete(self, identifier):
        self._ensure_started()
        with self.lock:
            status = self.get(identifier)
            if status["status"] in {"queued", "running", "cancelling"} or identifier in self.cancellations:
                raise ValueError("only an inactive finished job can be deleted")
            shutil.rmtree(self._folder(identifier))
            return {"id": identifier, "deleted": True}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for event in self.cancellations.values():
                event.set()
            executor = self.executor
        # The directory lease outlives all writers, including a last chunk.
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self.lock:
            if self.owner_file is not None:
                self.owner_file.close(); self.owner_file = None
