"""Verify wheel contents outside the checkout using this interpreter's dependencies.

The positional wheel argument is unchanged. This is an ASGI/runtime smoke check,
not a clean pip installation, console-entry-point check, or numerical validation.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

EXPECTED_VERSION = "0.2.0"

# Execute in a fresh process, with only the extracted wheel on PYTHONPATH. Keep
# the checks here so the test cannot accidentally import the checkout's package.
SMOKE_CODE = r'''
from copy import deepcopy
import importlib
import json
import math
import pathlib
import re
import time

import oceanroute
from fastapi.testclient import TestClient
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint

root = pathlib.Path.cwd().resolve()
started = time.monotonic()
modules = {}
for name in ("oceanroute", "oceanroute.api", "oceanroute.storage",
             "oceanroute.workspace", "oceanroute.workspace_storage",
             "oceanroute.seismic", "oceanroute.voyage", "oceanroute.voyage_jobs",
             "oceanroute.checkpoints"):
    module = importlib.import_module(name)
    location = pathlib.Path(module.__file__).resolve()
    assert location.is_relative_to(root), (name, str(location), str(root))
    modules[name] = str(location.relative_to(root))
assert oceanroute.__version__ == "0.2.0", oceanroute.__version__


def finite(value):
    json.dumps(value, allow_nan=False)
    return value


def get_json(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:1000])
    return finite(response.json())


def post_json(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 200, (path, response.status_code, response.text[:1000])
    return finite(response.json())


def wait_completed(client, identifier, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = get_json(client, "/api/voyage/jobs/" + identifier)
        if job["status"] not in {"queued", "running", "cancelling"}:
            assert job["status"] == "completed", job
            assert job["checkpoint_available"] and job["result_available"], job
            return job
        time.sleep(.02)
    raise AssertionError("Actual background job did not complete within the smoke deadline: " + identifier)


store_path = root / "data" / "smoke.sqlite3"
app = create_app(ProjectStore(store_path))
# The context manager actually enters lifespan, starts the directory owner and
# background worker, then waits for shutdown and releases the ownership lock.
with TestClient(app) as client:
    health = get_json(client, "/api/health")
    assert health["status"] == "ok" and health["version"] == "0.2.0", health
    page = client.get("/")
    assert page.status_code == 200 and 'id="root"' in page.text, page.text[:1000]
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
    assert assets, page.text
    for path in assets:
        assert client.get(path).status_code == 200, path
    manual = get_json(client, "/api/manual")
    assert "用户手册" in manual["text"], manual

    # Keep legacy schema1 analysis and optimistic-lock checks from the 0.1 smoke.
    project = get_json(client, "/api/sample")
    assert project["schema_version"] == 1, project.get("schema_version")
    analysis = post_json(client, "/api/analyze", project)
    assert analysis["summary"]["surface_length_m"] > 0
    post_json(client, "/api/projects", project)
    conflict = client.post("/api/projects", json=project)
    assert conflict.status_code in (409, 422), conflict.text

    # Schema2 is the source of truth: migration, shared alternative manufacture,
    # save/open, stale-write guard and an explicitly versioned full restore.
    migrated = post_json(client, "/api/workspace/migrate", {"project": project})
    workspace = migrated["workspace"]
    assert workspace["schema_version"] == 2
    assert len(workspace["paths"]) == len(workspace["assemblies"]) == 1
    assert workspace["active_path_id"] == migrated["project"]["id"]
    assert "cable_types" not in workspace["paths"][0]["project"]
    aggregate = post_json(client, "/api/workspace/analyze", workspace)
    manufactured = aggregate["summary"]["manufactured_total_m"]
    assert manufactured > 0 and aggregate["summary"]["deployment_path_count"] == 1
    first = post_json(client, "/api/workspaces", workspace)
    saved = first["workspace"]
    assert first["revision"] == saved["saved_revision"] == 1
    identifier = saved["id"]
    assert get_json(client, "/api/workspaces/" + identifier) == saved
    copied = post_json(client, "/api/workspace/action", {
        "workspace": saved, "config": {"action": "copy_path",
        "name": "Wheel smoke synthetic alternative", "assembly_policy": "alternative"}})
    assert len(copied["workspace"]["paths"]) == 2
    assert len(copied["workspace"]["assemblies"]) == 1
    assert copied["analysis"]["summary"]["alternative_path_count"] == 1
    assert math.isclose(copied["analysis"]["summary"]["manufactured_total_m"], manufactured,
                        rel_tol=1e-10, abs_tol=1e-7)
    second = post_json(client, "/api/workspaces", copied["workspace"])
    assert second["revision"] == second["workspace"]["saved_revision"] == 2
    stale = client.post("/api/workspaces", json=saved)
    assert stale.status_code in (409, 422), stale.text
    revisions = get_json(client, "/api/workspaces/" + identifier + "/revisions")
    assert {r["revision"] for r in revisions} == {1, 2}, revisions
    stale_restore = client.post("/api/workspaces/" + identifier + "/restore/1",
                                json={"expected_revision": 1})
    assert stale_restore.status_code in (409, 422), stale_restore.text
    restored = post_json(client, "/api/workspaces/" + identifier + "/restore/1",
                         {"expected_revision": 2})
    assert restored["saved_revision"] == 3 and len(restored["paths"]) == 1

    # Synthetic observations are generated by the real forward operator. They
    # are explicitly synthetic, not field data, and truth is removed before fit.
    truth_config = {
        "reference_frame": {"kind": "local_enu", "origin_wgs84": [118, 22, 0]},
        "line": {"depth_m": 100, "wet_weight_n_m": 4, "diameter_m": .02,
                 "bottom_tension_n": 250, "nodes": 64},
        "snapshots": [{"id": "synthetic-smoke", "time_s": 0,
                       "vessel_position_m": [0, 0, 0], "ship_speed_m_s": 1,
                       "heading_deg": 90,
                       "observations": [{"id": "synthetic-" + str(i),
                                         "arc_from_vessel_m": arc,
                                         "sigma_m": [.25, .25, .25]}
                                        for i, arc in enumerate([20, 50, 80])]}],
        "current_m_s": [.25, .2]}
    predicted = post_json(client, "/api/seismic/predict", {"config": truth_config})
    assert len(predicted["snapshots"][0]["nodes"]) == 64
    inverse_config = deepcopy(truth_config)
    inverse_config.pop("current_m_s")
    inverse_config["initial_current_m_s"] = [0, 0]
    for row, forward in zip(inverse_config["snapshots"], predicted["snapshots"]):
        for observation, calculated in zip(row["observations"], forward["observations"]):
            observation["position_m"] = calculated["predicted_position_m"]
    fitted = post_json(client, "/api/seismic/estimate", {"config": inverse_config})
    assert fitted["summary"]["estimate_accepted"] is True, fitted["summary"]
    assert fitted["identifiability"]["rank"] == 2
    assert fitted["summary"]["covariance_valid"] is True
    assert fitted["parameter_covariance_m2_s2"] is not None
    assert fitted["solver"]["forward_snapshot_solves"] > 0
    assert all(abs(value - expected) < 1e-6
               for value, expected in zip(fitted["estimated_current_m_s"], [.25, .2]))

    # Small, real background physics; no route-to-dynamics inference or long-time
    # accuracy/coarsening claim. Supply all material defaults independently.
    simulation = {"depth_m": 10, "wet_weight_n_m": 4, "bottom_tension_n": 10,
                  "nodes": 16, "ship_speed_m_s": .5, "payout_m_s": .6,
                  "heading_deg": 90, "current_x_m_s": 0, "current_y_m_s": 0,
                  "diameter_m": .02, "mass_kg_m": .7298997321841252,
                  "drag_coefficient": 1.2, "water_density_kg_m3": 1025,
                  "added_mass_coefficient": 1, "damping_ratio": .03,
                  "seabed_friction": .5, "ea_n": 1e8, "ei_n_m2": 0,
                  "max_tension_n": 1e12, "min_bend_radius_m": 0,
                  "internal_dt_s": .05, "dt_s": .5, "solver_iterations": 24}
    job = post_json(client, "/api/voyage/jobs", {"project": {}, "config": {
        "simulation": simulation, "duration_s": 2, "chunk_duration_s": 1,
        "adaptive_mesh": {"enabled": False}, "max_total_work_units": 2_000_000,
        "max_chunks": 8, "max_output_frames": 16, "max_mesh_records": 8}})
    parent_id = job["id"]
    parent = wait_completed(client, parent_id)
    checkpoint = get_json(client, "/api/voyage/jobs/" + parent_id + "/checkpoint")
    result = get_json(client, "/api/voyage/jobs/" + parent_id + "/result")
    read_voyage_checkpoint(checkpoint)
    assert checkpoint["schema"] == "oceanroute.voyage.checkpoint"
    assert checkpoint["physical_checkpoint"]["time_s"] == 2
    assert result["status"] == "completed" and result["summary"]["end_time_s"] == 2
    assert result["frames"][0]["time_s"] == 0 and result["frames"][-1]["time_s"] == 2
    assert all(len(frame["nodes"]) >= 16 for frame in result["frames"])
    assert abs(result["summary"]["material_balance_residual_m"]) < 1e-8
    assert math.isclose(result["summary"]["paid_out_m"], 1.2, abs_tol=1e-9)
    continuation = post_json(client, "/api/voyage/jobs/" + parent_id + "/resume", {
        "duration_s": 1, "chunk_duration_s": 1, "max_total_work_units": 2_000_000,
        "max_chunks": 8, "max_output_frames": 16, "max_mesh_records": 8})
    child_id = continuation["id"]
    child = wait_completed(client, child_id)
    assert child["parent_job_id"] == parent_id
    resumed = get_json(client, "/api/voyage/jobs/" + child_id + "/result")
    assert resumed["summary"]["start_time_s"] == 2
    assert resumed["summary"]["end_time_s"] == 3
    assert resumed["frames"][0]["time_s"] == 2 and resumed["frames"][-1]["time_s"] == 3
    assert math.isclose(resumed["summary"]["paid_out_m"], 1.8, abs_tol=1e-9)
    assert abs(resumed["summary"]["material_balance_residual_m"]) < 1e-8
    read_voyage_checkpoint(resumed["checkpoint"])

# A second app in the same directory proves lifespan released the writer lock
# and both completed jobs can actually be reloaded from durable files.
with TestClient(create_app(ProjectStore(store_path))) as reopened:
    recovered_parent = get_json(reopened, "/api/voyage/jobs/" + parent_id)
    recovered_child = get_json(reopened, "/api/voyage/jobs/" + child_id)
    assert recovered_parent["status"] == recovered_child["status"] == "completed"
    assert get_json(reopened, "/api/voyage/jobs/" + child_id + "/result")["summary"]["end_time_s"] == 3
    read_voyage_checkpoint(get_json(reopened, "/api/voyage/jobs/" + child_id + "/checkpoint"))
    assert get_json(reopened, "/api/workspaces/" + identifier)["saved_revision"] == 3

print(json.dumps(finite({
    "version": oceanroute.__version__, "wheel_module": str(oceanroute.__file__),
    "isolated_modules": modules, "assets": len(assets), "manual": True,
    "analysis": True, "revision_guard": True,
    "workspace": {"schema_version": 2, "migration": True, "shared_alternative": True,
                  "manufactured_total_m": manufactured, "saved_revision": 3,
                  "stale_save_rejected": True, "stale_restore_rejected": True,
                  "restored_path_count": 1},
    "seismic": {"observation_source": "synthetic_forward_model_no_noise",
                "transponders": 3, "nodes": 64, "estimate_accepted": True,
                "estimated_current_m_s": fitted["estimated_current_m_s"],
                "rank": 2, "covariance_valid": True},
    "voyage": {"completed_duration_s": 2, "resume_start_s": 2, "resume_end_s": 3,
               "paid_out_m": resumed["summary"]["paid_out_m"],
               "parent_frames": len(result["frames"]), "child_frames": len(resumed["frames"]),
               "checkpoint_validated": True, "durable_reopen": True,
               "writer_lifespan_reopened": True},
    "elapsed_s": time.monotonic() - started,
    "scope": "extracted wheel + existing interpreter dependencies + ASGI lifecycle; not clean install/browser/long-voyage certification"
})))
'''


def smoke(wheel: Path, report_path: Path | None = None) -> dict:
    wheel = wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="oceanroute-wheel-") as directory:
        destination = Path(directory)
        with zipfile.ZipFile(wheel) as package:
            metadata_paths = [name for name in package.namelist()
                              if name.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise ValueError("wheel must contain exactly one distribution METADATA")
            metadata = Parser().parsestr(package.read(metadata_paths[0]).decode("utf-8"))
            if metadata["Name"].lower() != "oceanroute" or metadata["Version"] != EXPECTED_VERSION:
                raise ValueError(f"expected OceanRoute {EXPECTED_VERSION}, got {metadata['Name']} {metadata['Version']}")
            package.extractall(destination)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(destination)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["OCEANROUTE_DATA_DIR"] = str(destination / "data")
        try:
            completed = subprocess.run([sys.executable, "-c", SMOKE_CODE], cwd=destination,
                                       env=environment, check=True, capture_output=True,
                                       text=True, timeout=120)
        except subprocess.CalledProcessError as error:
            if error.stdout:
                print(error.stdout, file=sys.stderr, end="")
            if error.stderr:
                print(error.stderr, file=sys.stderr, end="")
            raise RuntimeError(f"wheel smoke child failed with exit code {error.returncode}") from None
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        report = json.loads(completed.stdout)
        report["wheel_metadata_version"] = metadata["Version"]
        report["wheel"] = str(wheel)
        report["python_executable"] = sys.executable
        report["platform"] = sys.platform
        if report_path is not None:
            report_path = report_path.resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                              allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--report", type=Path, help="write the successful smoke result as JSON")
    options = parser.parse_args()
    smoke(options.wheel, options.report)
