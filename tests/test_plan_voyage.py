"""Manufacturing/geographic bridge checked against actual solver states."""
from copy import deepcopy
import json
import math
import time

from fastapi.testclient import TestClient
import numpy as np
import pytest

from oceanroute.api import create_app
from oceanroute.core import analyze_project
from oceanroute.geodesy import WGS84_A
from oceanroute.plan_voyage import _digest, prepare_plan_voyage, read_plan_mapping
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint, run_voyage


def project(length=300):
    return {"crs": "EPSG:4326", "route": {"curve": "rhumb", "slack_pct": 2,
            "points": [{"id": "a", "longitude": 0., "latitude": 0., "depth_m": 10},
                       {"id": "b", "longitude": math.degrees(length/WGS84_A), "latitude": 0., "depth_m": 10}],
            "legs": [{"cable_type_id": "A"}]},
            "cable_types": [{"id": "A", "lay_speed_m_s": .5, "wet_weight_n_m": 4,
                             "diameter_m": .02, "cost_per_m": 1, "ea_n": 1e6, "ei_n_m2": 0}], "bodies": []}


def options(duration=10):
    return {"plan": {"bottom_tension_n": 10, "sample_spacing_m": 30}, "duration_s": duration,
            "voyage": {"adaptive_mesh": {"enabled": False}, "chunk_duration_s": 3}}


def test_automatic_initial_inventory_is_not_paid_again():
    p = project()
    prepared = prepare_plan_voyage(p, options())
    actual = run_voyage(p, prepared["config"])
    mapping = prepared["mapping"]
    assert mapping["source_plan_start_s"] > 0
    assert mapping["manufacturing_origin_m"] == 0
    assert mapping["initial_manufacturing_top_m"] == pytest.approx(mapping["initial_natural_length_m"])
    assert actual["frames"][0]["node_material_m"][0] == pytest.approx(mapping["initial_manufacturing_top_m"])
    assert actual["frames"][-1]["node_material_m"][0] == pytest.approx(mapping["final_manufacturing_top_m"])
    assert actual["summary"]["paid_out_m"] == pytest.approx(5.1)
    assert actual["summary"]["paid_out_m"] != pytest.approx(mapping["final_manufacturing_top_m"])
    np.testing.assert_allclose(actual["frames"][-1]["ship"][:2], mapping["instructions"][-1]["vessel_end_xy_m"], atol=1e-8)
    np.testing.assert_allclose(actual["frames"][0]["touchdown"][:2], mapping["initial_anchor_xy_m"], atol=1e-8)
    assert mapping["initial_target_touchdown_residual_m"] > 1
    read_voyage_checkpoint(json.loads(json.dumps(actual["checkpoint"])))
    json.dumps(actual, allow_nan=False)


def test_stationary_allowance_keeps_kp_and_stock_coordinates_distinct():
    p = project()
    p["route"]["allowances"] = [{"kp_m": 20, "length_m": 5}]
    prepared = prepare_plan_voyage(p, options(70))
    intervals = prepared["mapping"]["instructions"]
    feeds = [r for r in intervals if r["kind"] == "allowance_feed"]
    assert len(feeds) == 1
    assert feeds[0]["route_start_kp_m"] == feeds[0]["route_end_kp_m"] == 20
    assert feeds[0]["manufacturing_end_m"]-feeds[0]["manufacturing_start_m"] == pytest.approx(5)
    np.testing.assert_allclose(feeds[0]["vessel_start_xy_m"], feeds[0]["vessel_end_xy_m"], atol=1e-8)
    actual = run_voyage(p, prepared["config"])
    assert actual["frames"][-1]["node_material_m"][0] == pytest.approx(prepared["mapping"]["final_manufacturing_top_m"])
    assert actual["summary"]["material_balance_residual_m"] < 1e-8


def test_turn_and_mixed_materials_reach_actual_projected_ship_endpoints():
    p = project(40)
    p["route"]["points"].append({"id": "c", "longitude": p["route"]["points"][-1]["longitude"], "latitude": .0005, "depth_m": 10})
    p["route"]["legs"].append({"cable_type_id": "B"})
    p["cable_types"].append({"id": "B", "lay_speed_m_s": .5, "wet_weight_n_m": 6,
                             "diameter_m": .03, "ea_n": 2e6, "cost_per_m": 2})
    prepared = prepare_plan_voyage(p, options(120))
    actual = run_voyage(p, prepared["config"])
    assert {r["id"] for r in prepared["config"]["simulation"]["material_segments"]} == {"A", "B"}
    assert any(r["kind"] == "offset_transition" for r in prepared["mapping"]["instructions"])
    np.testing.assert_allclose(actual["frames"][-1]["ship"][:2], prepared["mapping"]["instructions"][-1]["vessel_end_xy_m"], atol=1e-7)
    assert any(abs(w-6) < 1e-8 for w in actual["frames"][-1]["segment_wet_weight_n_m"])
    assert actual["frames"][-1]["node_material_m"][0] == pytest.approx(prepared["mapping"]["final_manufacturing_top_m"])


def test_point_body_loads_are_mapped_from_manufacturing_station():
    p = project()
    p["bodies"] = [{"id": "joint", "kp_m": 30, "length_m": 0, "cable_kp_m": 30,
                     "mass_kg": 10, "wet_weight_n": 30, "drag_area_m2": .1}]
    prepared = prepare_plan_voyage(p, options(50))
    assert prepared["config"]["simulation"]["inline_bodies"][0]["material_m"] == 30
    actual = run_voyage(p, prepared["config"])
    body = actual["frames"][-1]["inline_bodies"][0]
    assert body["deployed_fraction"] == pytest.approx(1)
    assert body["deployed_mass_kg"] == pytest.approx(10)


def test_prepared_mapping_persists_through_real_resume_and_clock():
    p = project()
    prepared = prepare_plan_voyage(p, options())
    first = run_voyage(p, {**prepared["config"], "duration_s": 4})
    second = run_voyage({}, {"resume_state": json.loads(json.dumps(first["checkpoint"])), "duration_s": 6})
    full = run_voyage(p, prepared["config"])
    assert second["plan_mapping"] == full["plan_mapping"]
    assert second["plan_mapping"]["source_plan_start_s"] + second["summary"]["end_time_s"] == pytest.approx(prepared["mapping"]["source_plan_end_s"])
    np.testing.assert_allclose(second["checkpoint"]["physical_checkpoint"]["state"]["positions"], full["checkpoint"]["physical_checkpoint"]["state"]["positions"], atol=1e-8)
    with pytest.raises(ValueError, match="prepared planning window"):
        run_voyage({}, {"resume_state": second["checkpoint"], "duration_s": 1})


def test_changed_project_or_physical_inputs_reject_stale_preparation():
    p = project()
    prepared = prepare_plan_voyage(p, options())
    changed = deepcopy(p)
    changed["route"]["slack_pct"] = 3
    with pytest.raises(ValueError, match="project no longer matches"):
        run_voyage(changed, prepared["config"])
    config = deepcopy(prepared["config"])
    config["simulation"]["payout_m_s"] += .1
    with pytest.raises(ValueError, match="no longer matches simulation"):
        run_voyage(p, config)
    mapping = deepcopy(prepared["mapping"])
    mapping["initial_manufacturing_top_m"] += 1
    with pytest.raises(ValueError, match="checksum"):
        read_plan_mapping(mapping)


def test_browser_integer_float_roundtrip_preserves_mapping_and_actual_state():
    def integer_equivalent(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [integer_equivalent(value) for value in item]
        if isinstance(item, dict):
            return {key: integer_equivalent(value) for key, value in item.items()}
        return item
    p = project()
    prepared = prepare_plan_voyage(p, options(2))
    config = integer_equivalent(json.loads(json.dumps(prepared["config"])))
    read_plan_mapping(config["plan_mapping"])
    actual = run_voyage(integer_equivalent(p), config)
    checkpoint = integer_equivalent(json.loads(json.dumps(actual["checkpoint"])))
    read_voyage_checkpoint(checkpoint)


@pytest.mark.parametrize("kind", ["missing-physics", "missing-depth", "changing-depth", "finite-body", "unmeasured-body", "extra-stock", "mixed-initial", "short-stock"])
def test_unsupported_or_incomplete_planning_data_is_not_guessed(kind):
    p, c = project(), options()
    if kind == "missing-physics":
        del p["cable_types"][0]["ea_n"]
    elif kind == "missing-depth":
        p["route"]["points"][0]["depth_m"] = None
    elif kind == "changing-depth":
        p["route"]["points"][1]["depth_m"] = 11
    elif kind == "finite-body":
        p["bodies"] = [{"id": "repeater", "kp_m": 50, "length_m": 2}]
    elif kind == "unmeasured-body":
        p["bodies"] = [{"id": "joint", "kp_m": 50, "length_m": 0}]
    elif kind == "extra-stock":
        c["plan"]["events"] = [{"kp_m": 40, "duration_s": 1, "payout_m_s": 1}]
    elif kind == "mixed-initial":
        p["route"]["allowances"] = [{"kp_m": 3, "length_m": 2, "cable_type_id": "B"}]
        p["cable_types"].append({"id": "B", "wet_weight_n_m": 6, "diameter_m": .03, "ea_n": 2e6})
    else:
        p = project(1)
    with pytest.raises(ValueError):
        prepare_plan_voyage(p, c)


@pytest.mark.parametrize("key,value", [("start_time_s", False), ("duration_s", float("nan")), ("max_track_span_m", 2), ("projection_tolerance_m", 0)])
def test_invalid_configuration_or_geographic_envelope_rejected(key, value):
    with pytest.raises(ValueError):
        prepare_plan_voyage(project(), {**options(), key: value})


def test_http_prepare_and_real_persistent_job_follow_manufacturing_clock(tmp_path):
    p = project()
    with TestClient(create_app(ProjectStore(tmp_path/"projects.sqlite3"))) as client:
        response = client.post("/api/shipplan/prepare-voyage", json={"project": p, "config": options(2)})
        assert response.status_code == 200, response.text
        prepared = response.json()
        config = {**prepared["config"], "duration_s": 1}
        response = client.post("/api/voyage/run", json={"project": p, "config": config})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["plan_mapping"]["route_signature"] == analyze_project(p)["route_signature"]
        assert result["frames"][-1]["node_material_m"][0] > result["frames"][0]["node_material_m"][0]
        corrupt = deepcopy(result["checkpoint"])
        corrupt["plan_mapping"]["origin_wgs84"][0] += 1
        with pytest.raises(ValueError, match="checksum"):
            read_voyage_checkpoint(corrupt)
        response = client.post("/api/voyage/jobs", json={"project": p, "config": config})
        assert response.status_code == 200, response.text
        identifier = response.json()["id"]
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            job = client.get("/api/voyage/jobs/"+identifier).json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        assert job["status"] == "completed", job
    with TestClient(create_app(ProjectStore(tmp_path/"projects.sqlite3"))) as reopened:
        saved = reopened.get("/api/voyage/jobs/"+identifier+"/result").json()
        assert saved["plan_mapping"] == result["plan_mapping"]
        response = reopened.post("/api/voyage/jobs/"+identifier+"/resume", json={"duration_s": 1})
        assert response.status_code == 200, response.text
        child_id = response.json()["id"]
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            job = reopened.get("/api/voyage/jobs/"+child_id).json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        assert job["status"] == "completed", job
        child = reopened.get("/api/voyage/jobs/"+child_id+"/result").json()
        assert child["plan_mapping"] == result["plan_mapping"]
        assert child["summary"]["end_time_s"] == 2
