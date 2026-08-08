"""Continuous actual state, finite budgets and durable background lifecycle."""
from copy import deepcopy
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.checkpoints import read_checkpoint
from oceanroute.simulation import simulate_lay
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint, run_voyage
from oceanroute.voyage_jobs import VoyageJobs


def settings():
    return {"depth_m": 10, "wet_weight_n_m": 4, "bottom_tension_n": 10,
            "nodes": 16, "ship_speed_m_s": .5, "payout_m_s": .6,
            "internal_dt_s": .05, "dt_s": 1, "solver_iterations": 24,
            "ship_plan_horizon_s": 20,
            "ship_plan": [{"time_s": 0, "speed_m_s": .5, "heading_deg": 90, "payout_m_s": .6},
                          {"time_s": 5.5, "speed_m_s": .4, "heading_deg": 92, "payout_m_s": .5},
                          {"time_s": 12, "speed_m_s": .45, "heading_deg": 89, "payout_m_s": .55}]}


def request(duration=20):
    return {"duration_s": duration, "chunk_duration_s": 4, "simulation": settings(),
            "adaptive_mesh": {"enabled": False}}


def state(result):
    return result["checkpoint"]["physical_checkpoint"]["state"]


def test_continuous_chunks_restore_actual_controls_material_and_velocity():
    result = run_voyage({}, request())
    reference = simulate_lay({}, {**settings(), "duration_s": 20})
    assert result["status"] == "completed"
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "last_segment_tensions_n"):
        np.testing.assert_allclose(state(result)[key], reference["checkpoint"]["state"][key], atol=2e-8, rtol=1e-9)
    assert result["summary"]["computed_duration_s"] == 20
    assert [r["end_time_s"] for r in result["chunks"]] == [4, 8, 12, 16, 20]
    assert result["summary"]["material_balance_residual_m"] < 1e-10


def test_saved_voyage_roundtrip_resumes_absolute_state_and_clock():
    first = run_voyage({}, request(10))
    restored = json.loads(json.dumps(first["checkpoint"]))
    assert read_voyage_checkpoint(restored)["physical_checkpoint"]["time_s"] == 10
    second = run_voyage({}, {"resume_state": restored, "duration_s": 10, "chunk_duration_s": 4})
    uninterrupted = run_voyage({}, request(20))
    assert second["summary"]["start_time_s"] == 10
    assert second["summary"]["end_time_s"] == 20
    np.testing.assert_allclose(state(second)["positions"], state(uninterrupted)["positions"], atol=2e-8)
    assert state(second)["paid_out_m"] == pytest.approx(state(uninterrupted)["paid_out_m"])


def test_cancel_at_completed_state_and_resume_does_not_fake_finish():
    completed = []
    result = run_voyage({}, request(), on_chunk=completed.append, should_cancel=lambda: len(completed) >= 2)
    assert result["status"] == "cancelled"
    assert result["summary"]["end_time_s"] == 8
    assert result["summary"]["requested_end_time_s"] == 20
    assert len(completed) == 2
    read_voyage_checkpoint(completed[-1]["checkpoint"])
    resumed = run_voyage({}, {"resume_state": result["checkpoint"], "duration_s": 2})
    assert resumed["summary"]["end_time_s"] == 10


def test_small_work_budget_stops_before_unfunded_solver_chunk():
    result = run_voyage({}, {**request(), "max_total_work_units": 1})
    assert result["status"] == "stopped"
    assert result["summary"]["chunks_this_run"] == 0
    assert result["checkpoint"] is None
    assert result["summary"]["estimated_work_units_this_run"] <= 1


def test_chunk_budget_and_retained_frames_are_real_samples():
    result = run_voyage({}, {**request(), "max_chunks": 2, "max_output_frames": 3})
    assert result["status"] == "stopped" and result["stop_reason"] == "chunk_budget"
    assert result["summary"]["end_time_s"] == 8
    assert len(result["frames"]) == 3
    assert result["frames"][0]["time_s"] == 0
    assert result["frames"][-1]["time_s"] == 8
    assert all(f["time_s"].is_integer() for f in result["frames"])


def test_voyage_checkpoint_detects_material_state_corruption():
    result = run_voyage({}, request(1))
    changed = deepcopy(result["checkpoint"])
    changed["physical_checkpoint"]["state"]["positions"][1][0] += .1
    with pytest.raises(ValueError, match="checksum"):
        read_voyage_checkpoint(changed)
    with pytest.raises(ValueError, match="restores physical controls"):
        run_voyage({}, {"resume_state": result["checkpoint"], "duration_s": 1, "simulation": {"payout_m_s": 1}})


def wait_job(client, identifier, *, checkpoint=False, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = client.get("/api/voyage/jobs/"+identifier)
        assert result.status_code == 200
        job = result.json()
        if checkpoint and job["checkpoint_available"]:
            return job
        if not checkpoint and job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(.01)
    pytest.fail("real local numerical job did not reach expected state")


def test_http_job_persists_result_checkpoint_and_can_resume(tmp_path):
    app = create_app(ProjectStore(tmp_path/"projects.sqlite3"))
    with TestClient(app) as client:
        response = client.post("/api/voyage/jobs", json={"project": {}, "config": request(2)})
        assert response.status_code == 200
        identifier = response.json()["id"]
        job = wait_job(client, identifier)
        assert job["status"] == "completed" and job["result_available"]
        checkpoint = client.get(f"/api/voyage/jobs/{identifier}/checkpoint").json()
        read_voyage_checkpoint(checkpoint)
        result = client.get(f"/api/voyage/jobs/{identifier}/result").json()
        assert result["summary"]["end_time_s"] == 2
        resumed = client.post(f"/api/voyage/jobs/{identifier}/resume", json={"duration_s": 1})
        assert resumed.status_code == 200
        successor = wait_job(client, resumed.json()["id"])
        assert successor["parent_job_id"] == identifier
        assert successor["summary"]["end_time_s"] == 3
        assert len(client.get("/api/voyage/jobs").json()) == 2
        assert client.post(f"/api/voyage/jobs/{identifier}/resume", json={"depth_m": 12}).status_code == 422
    reopened = VoyageJobs(tmp_path/"voyage_jobs")
    try:
        assert reopened.get(identifier)["status"] == "completed"
        assert reopened.checkpoint(identifier)["physical_checkpoint"]["time_s"] == 2
    finally:
        reopened.close()


def test_http_job_cancel_retains_actual_completed_checkpoint(tmp_path):
    app = create_app(ProjectStore(tmp_path/"projects.sqlite3"))
    with TestClient(app) as client:
        identifier = client.post("/api/voyage/jobs", json={"project": {}, "config": request(1000)}).json()["id"]
        wait_job(client, identifier, checkpoint=True)
        assert client.post(f"/api/voyage/jobs/{identifier}/cancel").status_code == 200
        job = wait_job(client, identifier)
        assert job["status"] == "cancelled"
        assert 0 < job["summary"]["computed_duration_s"] < 1000
        cp = client.get(f"/api/voyage/jobs/{identifier}/checkpoint").json()
        assert cp["physical_checkpoint"]["time_s"] == job["summary"]["end_time_s"]
        read_checkpoint(cp["physical_checkpoint"])


def test_http_bad_physics_fails_truthfully_and_invalid_path_is_rejected(tmp_path):
    app = create_app(ProjectStore(tmp_path/"projects.sqlite3"))
    with TestClient(app) as client:
        identifier = client.post("/api/voyage/jobs", json={"project": {}, "config": {"simulation": {"depth_m": -1}}}).json()["id"]
        job = wait_job(client, identifier)
        assert job["status"] == "failed"
        assert not job["checkpoint_available"] and not job["result_available"]
        assert "depth_m" in job["detail"]
        assert client.get("/api/voyage/jobs/not-a-uuid").status_code == 422
        assert client.get(f"/api/voyage/jobs/{identifier}/result").status_code == 404


def test_restart_marks_unfinished_job_interrupted_preserving_last_chunk(tmp_path):
    directory = tmp_path/"jobs"
    first = VoyageJobs(directory)
    result = run_voyage({}, request(1))
    from uuid import uuid4
    identifier = str(uuid4()); folder = directory/identifier; folder.mkdir()
    first._write(folder/"status.json", {"id": identifier, "status": "running", "created_at": "2026-01-01T00:00:00Z"})
    first._write(folder/"checkpoint.json", result["checkpoint"])
    first.close()
    second = VoyageJobs(directory)
    try:
        assert second.get(identifier)["status"] == "interrupted"
        assert second.checkpoint(identifier)["physical_checkpoint"]["time_s"] == 1
    finally:
        second.close()
