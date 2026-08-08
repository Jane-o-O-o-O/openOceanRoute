"""Real numerical jobs, abrupt process exit and durable lifecycle invariants.

Only filesystem/scheduling fault boundaries are injected. The voyage solver
and checkpoint readers remain the actual implementation in every job test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from oceanroute.voyage import read_voyage_checkpoint
from oceanroute.voyage_jobs import VoyageJobs


def config(duration=1.2):
    return {"duration_s": duration, "chunk_duration_s": .4,
            "adaptive_mesh": {"enabled": False},
            "simulation": {"depth_m": 10, "wet_weight_n_m": 4, "bottom_tension_n": 10,
                           "nodes": 16, "ship_speed_m_s": .5, "payout_m_s": .6,
                           "internal_dt_s": .05, "dt_s": .2, "solver_iterations": 24}}


def wait_terminal(manager, identifier, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        status = manager.get(identifier)
        if status["status"] not in {"queued", "running", "cancelling"}:
            return status
        time.sleep(.005)
    pytest.fail(f"real job {identifier} did not finish: {manager.get(identifier)}")


def wait_workers(manager):
    manager.executor.shutdown(wait=True)


def wait_retired(manager, identifier):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        with manager.lock:
            if identifier not in manager.cancellations:
                return
        time.sleep(.005)
    pytest.fail("terminal writer did not retire")


def test_atomic_replace_failure_retains_previous_finite_document(tmp_path, monkeypatch):
    target = tmp_path/"status.json"
    previous = {"status": "running", "progress": {"time_s": .4}}
    VoyageJobs._write(target, previous)
    replace = os.replace

    def fail_target(source, destination):
        if Path(destination) == target:
            raise OSError("injected rename failure")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_target)
    with pytest.raises(OSError, match="injected"):
        VoyageJobs._write(target, {"status": "completed", "progress": {"time_s": 1.2}})
    assert json.loads(target.read_text()) == previous


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX durability invariant")
def test_atomic_publication_flushes_parent_directory_on_posix(tmp_path, monkeypatch):
    original = os.fsync
    synced_directories = []

    def observe_real_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced_directories.append(descriptor)
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", observe_real_fsync)
    VoyageJobs._write(tmp_path/"published.json", {"actual": True})
    assert synced_directories, "file fsync alone does not flush the directory entry created by rename"
    assert json.loads((tmp_path/"published.json").read_text()) == {"actual": True}


def test_killed_process_recovers_actual_checkpoint_before_old_status(tmp_path):
    directory = tmp_path/"abrupt-exit"
    script = r'''
import os, sys
from oceanroute.voyage_jobs import VoyageJobs
class ExitAfterCheckpoint(VoyageJobs):
    @staticmethod
    def _write(path, value, limit=64000000):
        VoyageJobs._write(path, value, limit)
        if path.name == "checkpoint.json":
            os._exit(73)
manager = ExitAfterCheckpoint(sys.argv[1])
request = {"duration_s":1.2,"chunk_duration_s":.4,"adaptive_mesh":{"enabled":False},
           "simulation":{"depth_m":10,"wet_weight_n_m":4,"bottom_tension_n":10,
                         "nodes":16,"ship_speed_m_s":.5,"payout_m_s":.6,
                         "internal_dt_s":.05,"dt_s":.2,"solver_iterations":24}}
manager.submit({}, request)
manager.executor.shutdown(wait=True)
'''
    process = subprocess.run([sys.executable, "-c", script, str(directory)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 73, process.stderr
    folders = [p for p in directory.iterdir() if p.is_dir()]
    assert len(folders) == 1
    identifier = folders[0].name
    physical = read_voyage_checkpoint(json.loads((folders[0]/"checkpoint.json").read_text()))
    assert physical["physical_checkpoint"]["time_s"] == pytest.approx(.4)
    manager = VoyageJobs(directory)
    try:
        status = manager.get(identifier)
        assert status["status"] == "interrupted"
        assert status["checkpoint_available"], "durable checkpoint must not be hidden by stale pre-chunk status"
        assert status["completed_chunks"] == physical["completed_chunks"]
        assert status["progress"]["time_s"] == pytest.approx(.4)
        successor = manager.resume(identifier, {"duration_s": .4})
        done = wait_terminal(manager, successor["id"])
        assert done["status"] == "completed"
        assert done["parent_job_id"] == identifier
        assert done["summary"]["start_time_s"] == pytest.approx(.4)
        assert done["summary"]["end_time_s"] == pytest.approx(.8)
    finally:
        manager.close(); wait_workers(manager)


def test_process_exit_after_complete_result_recovers_terminal_publication(tmp_path):
    directory = tmp_path/"result-exit"
    script = r'''
import os, sys
from oceanroute.voyage_jobs import VoyageJobs
class ExitAfterResult(VoyageJobs):
    @staticmethod
    def _write(path, value, limit=64000000):
        VoyageJobs._write(path, value, limit)
        if path.name == "result.json":
            os._exit(74)
manager = ExitAfterResult(sys.argv[1])
request = {"duration_s":.8,"chunk_duration_s":.4,"adaptive_mesh":{"enabled":False},
           "simulation":{"depth_m":10,"wet_weight_n_m":4,"bottom_tension_n":10,
                         "nodes":16,"ship_speed_m_s":.5,"payout_m_s":.6,
                         "internal_dt_s":.05,"dt_s":.2,"solver_iterations":24}}
manager.submit({}, request)
manager.executor.shutdown(wait=True)
'''
    process = subprocess.run([sys.executable, "-c", script, str(directory)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 74, process.stderr
    identifier = next(p.name for p in directory.iterdir() if p.is_dir())
    durable = json.loads((directory/identifier/"result.json").read_text())
    assert durable["status"] == "completed" and durable["summary"]["end_time_s"] == pytest.approx(.8)
    manager = VoyageJobs(directory)
    try:
        status = manager.get(identifier)
        assert status["status"] == "completed" and status["result_available"]
        assert status["summary"]["end_time_s"] == pytest.approx(.8)
        checkpoint = manager.checkpoint(identifier)
        assert checkpoint["physical_checkpoint"]["time_s"] == pytest.approx(.8)
    finally:
        manager.close(); wait_workers(manager)


def test_second_process_cannot_take_directory_or_interrupt_live_job(tmp_path, monkeypatch):
    directory = tmp_path/"single-owner"
    manager = VoyageJobs(directory)
    ready, release = threading.Event(), threading.Event()
    original = manager._read

    def hold_before_physics(path):
        value = original(path)
        if Path(path).name == "request.json" and not ready.is_set():
            ready.set()
            if not release.wait(15):
                raise RuntimeError("test scheduling gate expired")
        return value

    monkeypatch.setattr(manager, "_read", hold_before_physics)
    script = r'''
import sys
from oceanroute.voyage_jobs import VoyageJobs
try:
    other = VoyageJobs(sys.argv[1])
except (ValueError, RuntimeError, OSError) as error:
    print("OWNER_REJECTED", str(error), flush=True)
    sys.exit(73)
else:
    other.close()
    print("SECOND_OWNER_ACCEPTED", flush=True)
'''
    try:
        submitted = manager.submit({}, config())
        assert ready.wait(5)
        process = subprocess.run([sys.executable, "-c", script, str(directory)],
                                 capture_output=True, text=True, timeout=10)
        assert process.returncode == 73 and "OWNER_REJECTED" in process.stdout, process.stdout+process.stderr
        assert manager.get(submitted["id"])["status"] == "running"
        release.set()
        assert wait_terminal(manager, submitted["id"])["status"] == "completed"
    finally:
        release.set(); manager.close(); wait_workers(manager)


def test_close_keeps_directory_owned_until_queued_writers_have_exited(tmp_path, monkeypatch):
    directory = tmp_path/"closing-owner"
    manager = VoyageJobs(directory)
    ready, release = threading.Event(), threading.Event()
    original = manager._read

    def hold_request(path):
        value = original(path)
        if Path(path).name == "request.json" and not ready.is_set():
            ready.set()
            if not release.wait(15):
                raise RuntimeError("test scheduling gate expired")
        return value

    monkeypatch.setattr(manager, "_read", hold_request)
    closer = None
    try:
        submitted = manager.submit({}, config())
        assert ready.wait(5)
        cancelled = manager.cancellations[submitted["id"]]
        closer = threading.Thread(target=manager.close)
        closer.start()
        assert cancelled.wait(5)
        script = r'''
import sys
from oceanroute.voyage_jobs import VoyageJobs
try:
    manager = VoyageJobs(sys.argv[1])
except (ValueError, RuntimeError, OSError):
    sys.exit(73)
else:
    manager.close()
    sys.exit(0)
'''
        process = subprocess.run([sys.executable, "-c", script, str(directory)],
                                 capture_output=True, text=True, timeout=10)
        assert process.returncode == 73, "close must not expose the directory while the previous writer still exists"
        release.set(); closer.join(5)
        assert not closer.is_alive()
        assert manager.get(submitted["id"])["status"] == "cancelled"
        reopened = VoyageJobs(directory)
        try:
            assert reopened.get(submitted["id"])["status"] == "cancelled"
        finally:
            reopened.close(); wait_workers(reopened)
    finally:
        release.set()
        if closer is not None:
            closer.join(5)
        manager.close(); wait_workers(manager)


def test_queue_bound_and_cancelled_queued_job_do_not_compute_or_fake_checkpoint(tmp_path, monkeypatch):
    manager = VoyageJobs(tmp_path/"queue")
    ready, release = threading.Event(), threading.Event()
    original = manager._read

    def hold_first(path):
        value = original(path)
        if Path(path).name == "request.json" and not ready.is_set():
            ready.set()
            if not release.wait(10):
                raise RuntimeError("test scheduling gate expired")
        return value

    monkeypatch.setattr(manager, "_read", hold_first)
    try:
        first = manager.submit({}, config())
        assert ready.wait(5)
        with pytest.raises(ValueError, match="inactive|finished|active"):
            manager.delete(first["id"])
        queued = [manager.submit({}, config()) for _ in range(3)]
        with pytest.raises(ValueError, match="four|queue|capacity"):
            manager.submit({}, config())
        assert len(manager.list()) == 4
        cancelled = manager.cancel(queued[0]["id"])
        assert cancelled["status"] in {"cancelled", "cancelling"}
        release.set()
        assert wait_terminal(manager, first["id"])["status"] == "completed"
        status = wait_terminal(manager, queued[0]["id"])
        assert status["status"] == "cancelled"
        assert not status["checkpoint_available"] and status["completed_chunks"] == 0
        result = manager.result(queued[0]["id"])
        assert result["frames"] == [] and result["checkpoint"] is None
        assert result["summary"]["computed_duration_s"] == 0
        for job in queued[1:]:
            assert wait_terminal(manager, job["id"])["status"] == "completed"
    finally:
        release.set(); manager.close(); wait_workers(manager)


def test_cancel_after_terminal_publication_is_idempotent_before_worker_cleanup(tmp_path, monkeypatch):
    manager = VoyageJobs(tmp_path/"terminal-cancel")
    original = manager._write
    observed = []

    def cancel_at_publication(path, value, limit=64_000_000):
        original(path, value, limit)
        if Path(path).name == "status.json" and value.get("status") == "completed" and not observed:
            # Same legal state window as a concurrent cancel immediately after
            # terminal publication but before finally removes its Event.
            observed.append(manager.cancel(value["id"]))

    monkeypatch.setattr(manager, "_write", cancel_at_publication)
    try:
        submitted = manager.submit({}, config())
        wait_workers(manager)
        assert observed and observed[0]["status"] == "completed"
        assert manager.get(submitted["id"])["status"] == "completed"
        assert manager.result(submitted["id"])["summary"]["end_time_s"] == pytest.approx(1.2)
    finally:
        manager.close()


def test_final_result_write_failure_keeps_real_checkpoint_and_truthful_failure(tmp_path, monkeypatch):
    manager = VoyageJobs(tmp_path/"failed-output")
    original = manager._write

    def result_disk_failure(path, value, limit=64_000_000):
        if Path(path).name == "result.json":
            raise OSError("injected final output disk failure")
        original(path, value, limit)

    monkeypatch.setattr(manager, "_write", result_disk_failure)
    try:
        submitted = manager.submit({}, config())
        status = wait_terminal(manager, submitted["id"])
        assert status["status"] == "failed" and "injected" in status["detail"]
        assert status["checkpoint_available"] and not status["result_available"]
        checkpoint = manager.checkpoint(submitted["id"])
        assert checkpoint["physical_checkpoint"]["time_s"] == pytest.approx(1.2)
        assert checkpoint["completed_chunks"] == 3
        with pytest.raises(KeyError):
            manager.result(submitted["id"])
        monkeypatch.setattr(manager, "_write", original)
        successor = manager.resume(submitted["id"], {"duration_s": .4})
        assert wait_terminal(manager, successor["id"])["summary"]["end_time_s"] == pytest.approx(1.6)
    finally:
        manager.close(); wait_workers(manager)


def test_closed_manager_rejects_submit_before_creating_durable_orphan(tmp_path):
    manager = VoyageJobs(tmp_path/"closed")
    completed = manager.submit({}, config(.4))
    assert wait_terminal(manager, completed["id"])["status"] == "completed"
    manager.close(); wait_workers(manager)
    before = set(manager.directory.iterdir())
    with pytest.raises((ValueError, RuntimeError)):
        manager.submit({}, config())
    assert set(manager.directory.iterdir()) == before
    assert len(manager.list()) == 1
    assert not manager.cancellations


@pytest.mark.parametrize("identifier", ["../outside", "not-a-uuid", "00000000-0000-0000-0000-000000000000/../other", "{00000000-0000-0000-0000-000000000000}"])
def test_noncanonical_job_id_cannot_select_files(tmp_path, identifier):
    manager = VoyageJobs(tmp_path/"ids")
    try:
        for function in (manager.get, manager.cancel, manager.checkpoint, manager.result):
            with pytest.raises(ValueError, match="UUID|uuid|canonical"):
                function(identifier)
        with pytest.raises(ValueError):
            manager.resume(identifier, {"duration_s": .4})
    finally:
        manager.close(); wait_workers(manager)


def test_uuid_directory_symlink_does_not_escape_job_root(tmp_path):
    manager = VoyageJobs(tmp_path/"links")
    foreign = tmp_path/"foreign"; foreign.mkdir()
    identifier = str(uuid4())
    (foreign/"status.json").write_text(json.dumps({"id": identifier, "status": "completed", "created_at": "2026-01-01"}))
    try:
        try:
            (manager.directory/identifier).symlink_to(foreign, target_is_directory=True)
        except OSError:
            pytest.skip("host does not permit directory symlinks")
        with pytest.raises((ValueError, KeyError)):
            manager.get(identifier)
        assert all(row["id"] != identifier for row in manager.list())
    finally:
        manager.close(); wait_workers(manager)


def test_json_file_symlink_does_not_expose_external_content(tmp_path):
    manager = VoyageJobs(tmp_path/"file-links")
    try:
        submitted = manager.submit({}, config(.4))
        assert wait_terminal(manager, submitted["id"])["status"] == "completed"
        foreign = tmp_path/"external-result.json"; foreign.write_text('{"external_marker":true}')
        result = manager.directory/submitted["id"]/"result.json"
        result.unlink()
        try:
            result.symlink_to(foreign)
        except OSError:
            pytest.skip("host does not permit file symlinks")
        with pytest.raises((ValueError, KeyError)):
            manager.result(submitted["id"])
    finally:
        manager.close(); wait_workers(manager)


def test_corrupt_task_metadata_does_not_prevent_healthy_tasks_from_reopening(tmp_path):
    directory = tmp_path/"corrupt-metadata"
    manager = VoyageJobs(directory)
    healthy = manager.submit({}, config(.4))
    assert wait_terminal(manager, healthy["id"])["status"] == "completed"
    manager.close(); wait_workers(manager)
    damaged = directory/str(uuid4()); damaged.mkdir()
    (damaged/"status.json").write_text('{"status":')
    reopened = VoyageJobs(directory)
    try:
        assert reopened.get(healthy["id"])["status"] == "completed"
        assert reopened.checkpoint(healthy["id"])["physical_checkpoint"]["time_s"] == pytest.approx(.4)
        assert any(row["id"] == healthy["id"] for row in reopened.list())
    finally:
        reopened.close(); wait_workers(reopened)


@pytest.mark.parametrize("metadata", [{"status": "completed"},
    {"status": [], "created_at": "2026-01-01"},
    {"status": "running", "created_at": "2026-01-01", "progress": [1]}])
def test_semantically_invalid_metadata_is_isolated_without_losing_healthy_state(tmp_path, metadata):
    directory = tmp_path/"invalid-metadata"
    manager = VoyageJobs(directory)
    healthy = manager.submit({}, config(.4))
    assert wait_terminal(manager, healthy["id"])["status"] == "completed"
    manager.close(); wait_workers(manager)
    damaged = directory/str(uuid4()); damaged.mkdir()
    (damaged/"status.json").write_text(json.dumps(metadata))
    (damaged/"checkpoint.json").write_bytes((directory/healthy["id"]/"checkpoint.json").read_bytes())
    reopened = VoyageJobs(directory)
    try:
        assert reopened.get(healthy["id"])["status"] == "completed"
        rows = reopened.list()
        assert any(row["id"] == healthy["id"] for row in rows)
        json.dumps(rows, allow_nan=False)
    finally:
        reopened.close(); wait_workers(reopened)


def test_saved_job_quota_and_explicit_inactive_delete_free_capacity(tmp_path):
    manager = VoyageJobs(tmp_path/"saved-limit", max_saved_jobs=1)
    try:
        first = manager.submit({}, config(.4))
        assert wait_terminal(manager, first["id"])["status"] == "completed"
        wait_retired(manager, first["id"])
        folders = set(manager._folders())
        with pytest.raises(ValueError, match="capacity|quota|saved"):
            manager.submit({}, config(.4))
        assert set(manager._folders()) == folders
        assert manager.delete(first["id"]) == {"id": first["id"], "deleted": True}
        assert manager.list() == []
        with pytest.raises(KeyError):
            manager.checkpoint(first["id"])
        next_job = manager.submit({}, config(.4))
        assert next_job["id"] != first["id"]
        assert wait_terminal(manager, next_job["id"])["status"] == "completed"
    finally:
        manager.close(); wait_workers(manager)


def test_disk_quota_preserves_existing_results_and_last_checkpoint_then_delete_frees_space(tmp_path):
    manager = VoyageJobs(tmp_path/"disk-limit", max_storage_bytes=60_000)
    try:
        first = manager.submit({}, config(.4))
        assert wait_terminal(manager, first["id"])["status"] == "completed"
        wait_retired(manager, first["id"])
        original = manager.result(first["id"])
        second = manager.submit({}, config(.4))
        failed = wait_terminal(manager, second["id"])
        assert failed["status"] == "failed" and "quota" in failed["detail"]
        assert failed["checkpoint_available"] and not failed["result_available"]
        assert manager.checkpoint(second["id"])["physical_checkpoint"]["time_s"] == pytest.approx(.4)
        assert manager.result(first["id"]) == original
        with pytest.raises(KeyError):
            manager.result(second["id"])
        used = sum(p.stat().st_size for folder in manager._folders() for p in folder.iterdir() if p.is_file())
        assert used <= manager.max_storage_bytes
        folders = set(manager._folders())
        with pytest.raises(ValueError, match="quota"):
            manager.submit({}, {**config(.4), "source": "x"*25_000})
        assert set(manager._folders()) == folders
        wait_retired(manager, second["id"])
        manager.delete(first["id"])
        next_job = manager.submit({}, config(.4))
        assert wait_terminal(manager, next_job["id"])["status"] == "completed"
        assert manager.checkpoint(second["id"])["physical_checkpoint"]["time_s"] == pytest.approx(.4)
    finally:
        manager.close(); wait_workers(manager)


def test_existing_history_over_new_quota_remains_readable_and_can_be_deleted(tmp_path):
    directory = tmp_path/"lowered-quota"
    manager = VoyageJobs(directory)
    submitted = manager.submit({}, config(.4))
    assert wait_terminal(manager, submitted["id"])["status"] == "completed"
    manager.close(); wait_workers(manager)
    reopened = VoyageJobs(directory, max_storage_bytes=10_000)
    try:
        assert reopened.get(submitted["id"])["status"] == "completed"
        assert reopened.checkpoint(submitted["id"])["physical_checkpoint"]["time_s"] == pytest.approx(.4)
        assert len(reopened.list()) == 1
        with pytest.raises(ValueError, match="quota"):
            reopened.submit({}, config(.4))
        assert len(reopened.list()) == 1
        assert reopened.delete(submitted["id"])["deleted"]
        assert reopened.list() == []
    finally:
        reopened.close(); wait_workers(reopened)


def test_oversized_or_nonfinite_request_rejected_before_any_job_directory(tmp_path):
    manager = VoyageJobs(tmp_path/"input-budget")
    try:
        before = set(manager.directory.iterdir())
        huge = {**config(), "source": "x"*32_000_001}
        with pytest.raises(ValueError, match="volume|limit|exceeds"):
            manager.submit({}, huge)
        with pytest.raises(ValueError, match="finite|JSON"):
            manager.submit({}, {**config(), "source": float("nan")})
        assert set(manager.directory.iterdir()) == before
        assert manager.list() == []
    finally:
        manager.close(); wait_workers(manager)


def test_http_job_identity_input_shape_and_real_download_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("OCEANROUTE_DATA_DIR", str(tmp_path/"unused-global-app"))
    from fastapi.testclient import TestClient
    from oceanroute.api import create_app
    from oceanroute.storage import ProjectStore
    app = create_app(ProjectStore(tmp_path/"projects.sqlite3"))
    with TestClient(app) as client:
        for endpoint in ("/api/voyage/run", "/api/voyage/jobs"):
            for invalid in ([], None, "bad"):
                response = client.post(endpoint, json={"project": invalid, "config": config(.4)})
                assert response.status_code == 422, (endpoint, invalid, response.text)
            assert client.post(endpoint, json={"project": {}, "config": []}).status_code == 422
        absent = str(uuid4())
        for identifier, expected in (("not-a-uuid", 422), (absent, 404)):
            for suffix in ("", "/checkpoint", "/result"):
                assert client.get(f"/api/voyage/jobs/{identifier}{suffix}").status_code == expected
            assert client.post(f"/api/voyage/jobs/{identifier}/cancel").status_code == expected
            assert client.post(f"/api/voyage/jobs/{identifier}/resume", json={"duration_s": .4}).status_code == expected
        submitted = client.post("/api/voyage/jobs", json={"project": {}, "config": config(.8)})
        assert submitted.status_code == 200, submitted.text
        identifier = submitted.json()["id"]
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            row = client.get(f"/api/voyage/jobs/{identifier}").json()
            if row["status"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(.005)
        assert row["status"] == "completed" and row["checkpoint_available"] and row["result_available"]
        result = client.get(f"/api/voyage/jobs/{identifier}/result")
        checkpoint = client.get(f"/api/voyage/jobs/{identifier}/checkpoint")
        assert result.status_code == checkpoint.status_code == 200
        json.dumps(result.json(), allow_nan=False)
        assert read_voyage_checkpoint(checkpoint.json())["physical_checkpoint"]["time_s"] == pytest.approx(.8)
        assert result.json()["frames"][-1]["time_s"] == pytest.approx(.8)
        assert client.post(f"/api/voyage/jobs/{identifier}/cancel").json()["status"] == "completed"
        wait_retired(app.state.voyage_jobs, identifier)
        removed = client.delete(f"/api/voyage/jobs/{identifier}")
        assert removed.status_code == 200 and removed.json()["deleted"]
        assert client.get(f"/api/voyage/jobs/{identifier}").status_code == 404
        assert client.get(f"/api/voyage/jobs/{identifier}/result").status_code == 404
        assert client.delete("/api/voyage/jobs/not-a-uuid").status_code == 422
        assert client.delete(f"/api/voyage/jobs/{identifier}").status_code == 404
