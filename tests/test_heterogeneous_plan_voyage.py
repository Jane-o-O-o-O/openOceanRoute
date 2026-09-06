"""Actual geographic preparation with independently balanced mixed cable.

The route is equatorial, so its two manufacturing intervals and Mercator
coordinates have closed formulae. The expected initial shape is generated
by discrete force balance, without the material or initialization helpers.
"""
from copy import deepcopy
import hashlib
import json
import math
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.plan_voyage import prepare_plan_voyage, read_plan_mapping
from oceanroute.shipplan import build_ship_plan
from oceanroute.storage import ProjectStore
from oceanroute.voyage import run_voyage
from oceanroute.workspace import materialize_path


RADIUS = 6378137.


def project(body_weight=30.):
    return {"crs": "EPSG:4326", "route": {"curve": "rhumb", "slack_pct": 2.,
        "points": [{"id": "a", "longitude": 0., "latitude": 0., "depth_m": 10.},
                   {"id": "b", "longitude": math.degrees(10/RADIUS), "latitude": 0., "depth_m": 10.},
                   {"id": "c", "longitude": math.degrees(300/RADIUS), "latitude": 0., "depth_m": 10.}],
        "legs": [{"cable_type_id": "A"}, {"cable_type_id": "B"}]},
        "cable_types": [
            {"id": "A", "lay_speed_m_s": .5, "wet_weight_n_m": 4., "diameter_m": .02,
             "cost_per_m": 1., "ea_n": 10000., "ei_n_m2": 0., "mass_kg_m": 1.2},
            {"id": "B", "lay_speed_m_s": .5, "wet_weight_n_m": 7., "diameter_m": .03,
             "cost_per_m": 2., "ea_n": 24000., "ei_n_m2": 0., "mass_kg_m": 1.8}],
        "bodies": [{"id": "point", "kp_m": 15., "cable_kp_m": 15.3, "length_m": 0.,
                    "mass_kg": 10., "wet_weight_n": body_weight, "drag_area_m2": .1}]}


def independent_state(body_weight):
    rest = np.array([12., 2., 3., 1., 4., 2., 3.])
    coordinates = 4.+np.r_[np.cumsum(rest[::-1])[::-1], 0.]
    a_length = np.array([max(0., min(high, 10.2)-max(low, 0.))
                         for high, low in zip(coordinates[:-1], coordinates[1:])])
    b_length = rest-a_length
    compliance = a_length/10000.+b_length/24000.
    ea = rest/compliance
    segment_weight = 4*a_length+7*b_length
    cable_weight = np.r_[segment_weight[0]/2,
                        (segment_weight[:-1]+segment_weight[1:])/2, segment_weight[-1]/2]
    segment_dry = 1.2*a_length+1.8*b_length
    cable_dry = np.r_[segment_dry[0]/2, (segment_dry[:-1]+segment_dry[1:])/2, segment_dry[-1]/2]
    alpha = np.zeros(len(rest)+1)
    for i, (high, low) in enumerate(zip(coordinates[:-1], coordinates[1:])):
        if low <= 15.3 <= high:
            alpha[i] = (15.3-low)/rest[i]
            alpha[i+1] = (high-15.3)/rest[i]
            break
    total_weight = cable_weight+body_weight*alpha
    vertical = np.empty(len(rest)); vertical[-1] = 15.
    for j in range(len(rest)-1, 0, -1):
        vertical[j-1] = vertical[j]+total_weight[j]
    tension = np.hypot(150., vertical)
    direction = np.column_stack((150./tension, np.zeros(len(rest)), vertical/tension))
    vectors = rest[:, None]*(1+tension[:, None]/ea[:, None])*direction
    positions = np.vstack(([0., 0., -2.], np.array([0., 0., -2.])-np.cumsum(vectors, axis=0)))
    return {"rest": rest, "coordinates": coordinates, "ea": ea, "weight": total_weight,
            "dry": cable_dry+10*alpha, "alpha": alpha, "positions": positions, "tension": tension,
            "vertical": vertical}


def fixture(body_weight=30.):
    p = project(body_weight)
    state = independent_state(body_weight)
    # The actual ship plan is an input to this bridge. Read its command with
    # material31, then independently locate that command on the equator.
    plan_options = {"bottom_tension_n": 10., "sample_spacing_m": 30.}
    plan = build_ship_plan(p, plan_options)
    row = next(r for r in plan["instructions"] if r["cable_start_m"] < 31 < r["cable_end_m"])
    fraction = (31-row["cable_start_m"])/(row["cable_end_m"]-row["cable_start_m"])
    start = row["time_s"]+fraction*row["duration_s"]
    lon = row["vessel_start"][0]+fraction*(row["vessel_end"][0]-row["vessel_start"][0])
    absolute = np.array([RADIUS*math.radians(lon), 0.])
    old_origin = np.array([11., -7.])
    shift = absolute-old_origin
    old_positions = state["positions"]+np.r_[shift, 0.]
    anchor = state["positions"][-1]+np.r_[absolute, 0.]
    c = {"plan": plan_options, "start_time_s": start, "duration_s": .08,
         "simulation": {"nodes": 8, "internal_dt_s": .002, "dt_s": .02, "solver_iterations": 32},
         "voyage": {"adaptive_mesh": {"enabled": False}, "chunk_duration_s": .04},
         "seabed_grid": {"schema": "oceanroute.bathymetry.v1",
             "x_m": [-200., 0., 200.], "y_m": [-100., 0., 100.], "z_m": [[-100.]*3 for _ in range(3)],
             "source": {"name": "mixed-material geographic test bed", "horizontal_crs": "EPSG:3857",
                        "origin_projected_m": old_origin.tolist(), "vertical_datum": "model sea zero"}},
         "equilibrium_start": {"anchor": {"longitude": math.degrees(anchor[0]/RADIUS),
             "latitude": 0., "z_model_m": anchor[2]}, "vessel_z_m": -2.,
             "rest_lengths_m": state["rest"].tolist(), "initial_positions_m": old_positions.tolist()}}
    return p, c, state, absolute, shift


@pytest.mark.parametrize("body_weight", [30., -50.])
def test_actual_geographic_prepare_preserves_mixed_material_and_signed_point_load(body_weight):
    p, c, expected, absolute, shift = fixture(body_weight)
    original = deepcopy((p, c))
    prepared = prepare_plan_voyage(p, c)
    proof = prepared["mapping"]["initial_equilibrium_preparation"]["provenance"]
    snap = proof["initial_snapshot"]
    assert proof["schema"] == "oceanroute.dynamic.initial-equilibrium.provenance.v2"
    assert np.asarray(snap["positions"]) == pytest.approx(expected["positions"], abs=1e-8)
    assert snap["segment_ea_n"] == pytest.approx(expected["ea"], rel=1e-12)
    assert snap["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-10)
    assert snap["node_dry_mass_kg"] == pytest.approx(expected["dry"], abs=1e-10)
    assert snap["node_material_m"] == pytest.approx(expected["coordinates"], abs=1e-12)
    assert snap["segment_tension_n"] == pytest.approx(expected["tension"], abs=1e-7)
    assert prepared["mapping"]["manufacturing_origin_m"] == pytest.approx(4., abs=1e-12)
    assert prepared["mapping"]["initial_manufacturing_top_m"] == pytest.approx(31., abs=1e-12)
    frame = prepared["mapping"]["terrain_frame"]
    assert frame["origin_projected_m"] == pytest.approx(absolute, abs=1e-9)
    assert frame["translation_from_original_local_m"] == pytest.approx(shift, abs=1e-9)
    assert (p, c) == original
    read_plan_mapping(json.loads(json.dumps(prepared["mapping"])), simulation=prepared["config"]["simulation"])


def test_real_plan_motion_and_feed_respond_then_resume_without_solving_initial_state_again(monkeypatch):
    p, c, expected, _, _ = fixture()
    prepared = prepare_plan_voyage(p, c)
    whole = run_voyage(p, prepared["config"])
    first = run_voyage(p, {**prepared["config"], "duration_s": .04})
    saved = json.loads(json.dumps(first["checkpoint"], allow_nan=False))
    import oceanroute.initial_equilibrium as initializer
    def forbidden(*args, **kwargs):
        raise AssertionError("resume must reconstruct force/material proof without static optimization")
    monkeypatch.setattr(initializer, "resolve_initial_equilibrium", forbidden)
    resumed = run_voyage({}, {"resume_state": saved, "duration_s": .04})
    physical = whole["checkpoint"]["physical_checkpoint"]["state"]
    resumed_state = resumed["checkpoint"]["physical_checkpoint"]["state"]
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_wet_weight_n"):
        assert np.asarray(resumed_state[key]) == pytest.approx(np.asarray(physical[key]), abs=1e-9)
    assert whole["frames"][0]["paid_out_m"] == 0.
    assert physical["paid_out_m"] == pytest.approx(.51*.08, abs=1e-12)
    assert physical["node_material_m"][0] == pytest.approx(31+.51*.08, abs=1e-12)
    assert np.max(np.linalg.norm(np.asarray(physical["positions"])[1:-1]-expected["positions"][1:-1], axis=1)) > 1e-7
    assert whole["frames"][0]["touchdown"] is None
    assert whole["frames"][-1]["inline_bodies"][0]["deployed_wet_weight_n"] == 30.


def checksum(document):
    def canonical(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            return [canonical(v) for v in value]
        return value
    return hashlib.sha256(json.dumps(canonical({k: v for k, v in document.items() if k != "checksum_sha256"}),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def test_rechecks_material_loading_after_mapping_checksum_is_recomputed():
    p, c, _, _, _ = fixture()
    mapping = prepare_plan_voyage(p, c)["mapping"]
    changed = deepcopy(mapping)
    changed["source_simulation"]["inline_bodies"][0]["wet_weight_n"] += 5.
    changed["checksum_sha256"] = checksum(changed)
    with pytest.raises(ValueError):
        read_plan_mapping(changed)


def test_real_http_geographic_prepare_and_run_support_mixed_material_initial_load(tmp_path):
    p, c, expected, _, _ = fixture(-50.)
    with TestClient(create_app(ProjectStore(tmp_path/"mixed-plan.sqlite3")), raise_server_exceptions=False) as client:
        response = client.post("/api/shipplan/prepare-voyage", json={"project": p, "config": c})
        assert response.status_code == 200, response.text
        prepared = response.json()
        result = client.post("/api/voyage/run", json={"project": p, "config": prepared["config"]})
        assert result.status_code == 200, result.text
        assert result.json()["frames"][0]["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-10)


def test_signed_point_body_survives_workspace_migration_disk_reopen_and_geographic_prepare(tmp_path):
    p, c, expected, _, _ = fixture(-50.)
    database = tmp_path/"signed-workspace.sqlite3"
    with TestClient(create_app(ProjectStore(database))) as client:
        migrated = client.post("/api/workspace/migrate", json={"project": p})
        assert migrated.status_code == 200, migrated.text
        workspace = migrated.json()["workspace"]
        body = next(i for i in workspace["assemblies"][0]["items"] if i["kind"] == "body")
        assert body["properties"]["wet_weight_n"] == -50.
        assert body["properties"]["mass_kg"] == 10.
        saved = client.post("/api/workspaces", json=workspace)
        assert saved.status_code == 200, saved.text
        frozen = saved.json()["workspace"]
    with TestClient(create_app(ProjectStore(database))) as client:
        reopened = client.get("/api/workspaces/"+frozen["id"])
        assert reopened.status_code == 200, reopened.text
        assert reopened.json() == frozen
        exported = client.post("/api/workspace/export", json=reopened.json())
        assert exported.status_code == 200, exported.text
        imported = client.post("/api/workspace/import", json={"text": exported.text})
        assert imported.status_code == 200, imported.text
        projected = materialize_path(imported.json()["workspace"])
        assert projected["bodies"][0]["wet_weight_n"] == -50.
        assert projected["bodies"][0]["mass_kg"] == 10.
        prepared = client.post("/api/shipplan/prepare-voyage", json={"project": projected, "config": c})
        assert prepared.status_code == 200, prepared.text
        snapshot = prepared.json()["mapping"]["initial_equilibrium_preparation"]["provenance"]["initial_snapshot"]
        assert snapshot["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-10)
        assert snapshot["node_dry_mass_kg"] == pytest.approx(expected["dry"], abs=1e-10)


@pytest.mark.parametrize("property_name", ["mass_kg", "diameter_m", "drag_area_m2", "drag_coefficient"])
def test_signed_wet_weight_support_does_not_allow_negative_inertia_or_geometry(tmp_path, property_name):
    p, _, _, _, _ = fixture(-50.)
    with TestClient(create_app(ProjectStore(tmp_path/"invalid-body.sqlite3"))) as client:
        migrated = client.post("/api/workspace/migrate", json={"project": p})
        assert migrated.status_code == 200, migrated.text
        invalid = migrated.json()["workspace"]
        body = next(i for i in invalid["assemblies"][0]["items"] if i["kind"] == "body")
        body["properties"][property_name] = -1.
        refused = client.post("/api/workspaces", json=invalid)
        assert refused.status_code == 422, refused.text
        assert property_name in refused.json()["detail"]
        assert client.get("/api/workspaces").json() == []


def terminal(client, identifier):
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        response = client.get(f"/api/voyage/jobs/{identifier}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] not in {"queued", "running", "cancelling"}:
            return value
        time.sleep(.01)
    pytest.fail("mixed-material geographic job did not finish")


def test_durable_geographic_job_reopens_and_continues_actual_signed_material_state(tmp_path):
    p, c, expected, _, _ = fixture(-50.)
    prepared = prepare_plan_voyage(p, c)
    whole = run_voyage(p, prepared["config"])
    database = tmp_path/"durable-mixed-plan.sqlite3"
    with TestClient(create_app(ProjectStore(database))) as client:
        response = client.post("/api/voyage/jobs", json={"project": p, "config": {
            **prepared["config"], "duration_s": .04}})
        assert response.status_code == 200, response.text
        identifier = response.json()["id"]
        assert terminal(client, identifier)["status"] == "completed"
        before = client.get(f"/api/voyage/jobs/{identifier}/checkpoint").json()
    with TestClient(create_app(ProjectStore(database))) as client:
        response = client.get(f"/api/voyage/jobs/{identifier}/checkpoint")
        assert response.status_code == 200, response.text
        assert response.json() == before
        response = client.post(f"/api/voyage/jobs/{identifier}/resume", json={"duration_s": .04})
        assert response.status_code == 200, response.text
        child = response.json()["id"]
        assert terminal(client, child)["status"] == "completed"
        response = client.get(f"/api/voyage/jobs/{child}/checkpoint")
        assert response.status_code == 200, response.text
        final = response.json()
    actual = final["physical_checkpoint"]["state"]
    expected_final = whole["checkpoint"]["physical_checkpoint"]["state"]
    assert final["plan_mapping"] == before["plan_mapping"]
    assert actual["initialization_provenance"] == before["physical_checkpoint"]["state"]["initialization_provenance"]
    assert actual["initialization_provenance"]["initial_snapshot"]["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-10)
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_wet_weight_n"):
        assert np.asarray(actual[key]) == pytest.approx(np.asarray(expected_final[key]), abs=1e-9)


@pytest.mark.parametrize("kind", ["legacy-analytic", "initial-ei", "finite-body"])
def test_existing_unmodeled_physics_is_still_rejected(kind):
    p, c, _, _, _ = fixture()
    if kind == "legacy-analytic":
        c.pop("equilibrium_start"); c.pop("seabed_grid")
        c.pop("start_time_s")
    elif kind == "initial-ei":
        p["cable_types"][0]["ei_n_m2"] = 1.
    else:
        p["bodies"][0]["length_m"] = 1.
    with pytest.raises(ValueError):
        prepare_plan_voyage(p, c)
