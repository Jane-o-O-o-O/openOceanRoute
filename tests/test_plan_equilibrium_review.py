"""Independent geographic-frame/material audit of equilibrium plan mapping.

Mercator expectations use analytic forward/inverse formulae. Initial stock is
the declared natural material; no tested projection or initialization helper
is used to generate expected coordinates, lengths, masses or physical state.
"""
from copy import deepcopy
import hashlib
import json
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.plan_equilibrium_frame import PlanBathymetryFrame
from oceanroute.plan_voyage import prepare_plan_voyage, read_plan_mapping
from oceanroute.shipplan import build_ship_plan
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint, run_voyage
from scipy.optimize import brentq


RADIUS = 6378137.


def mercator(lon, lat):
    return np.array([RADIUS*math.radians(lon),
        RADIUS*math.log(math.tan(math.pi/4+math.radians(lat)/2))])


def geographic(x, y):
    return [math.degrees(x/RADIUS), math.degrees(2*math.atan(math.exp(y/RADIUS))-math.pi/2)]


def checksum(document):
    def canonical(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        return value
    payload = {key: value for key, value in document.items() if key != "checksum_sha256"}
    return hashlib.sha256(json.dumps(canonical(payload), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def project():
    return {"crs": "EPSG:4326", "route": {"curve": "rhumb", "slack_pct": 2.,
        "points": [{"id": "a", "longitude": 0., "latitude": 0., "depth_m": 10.},
                   {"id": "b", "longitude": math.degrees(300/RADIUS), "latitude": 0., "depth_m": 11.}],
        "legs": [{"cable_type_id": "A"}]},
        "cable_types": [{"id": "A", "lay_speed_m_s": .5, "wet_weight_n_m": 4.,
            "diameter_m": .02, "cost_per_m": 1., "ea_n": 1e5, "ei_n_m2": 0.}], "bodies": []}


def grid():
    return {"schema": "oceanroute.bathymetry.v1", "x_m": [-200., 0., 200.], "y_m": [-100., 0., 100.],
        "z_m": [[-30., -29., -30.], [-30., -30., -30.], [-30., -31., -30.]],
        "source": {"name": "independent projected variable bed", "horizontal_crs": "EPSG:3857",
            "origin_projected_m": [0., 0.], "vertical_datum": "already aligned to model sea zero"}}


def configuration(**updates):
    c = {"plan": {"bottom_tension_n": 10., "sample_spacing_m": 30.}, "duration_s": .1,
        "simulation": {"nodes": 10, "internal_dt_s": .01, "dt_s": .02},
        "voyage": {"adaptive_mesh": {"enabled": False}, "chunk_duration_s": .04},
        "seabed_grid": grid(),
        "equilibrium_start": {"anchor": {"longitude": 0., "latitude": 0., "z_model_m": -10.},
            "vessel_z_m": 0., "natural_length_m": 20.}}
    c.update(updates)
    return c


def test_northern_mercator_frame_translates_entire_grid_seed_and_endpoints_without_z_change():
    lon, lat = 110., 30.
    absolute = mercator(lon, lat)
    shift = np.array([37., -19.])
    old_origin = absolute-shift
    document = grid()
    document["source"]["origin_projected_m"] = old_origin.tolist()
    anchor_xy = np.array([-9., 7.])
    anchor_geo = geographic(*(absolute+anchor_xy))
    rest = [6., 2., 5., 3., 4.]
    fractions = np.r_[0., np.cumsum(rest)]/20
    vessel_local = np.r_[shift, -2.]
    anchor_local = np.r_[shift+anchor_xy, -20.]
    seed = vessel_local+fractions[:, None]*(anchor_local-vessel_local)
    options = {"anchor": {"longitude": anchor_geo[0], "latitude": anchor_geo[1], "z_model_m": -20.},
        "vessel_z_m": -2., "rest_lengths_m": rest, "initial_positions_m": seed.tolist()}
    keep_document, keep_options = deepcopy(document), deepcopy(options)
    actual = PlanBathymetryFrame(document, {"longitude": lon, "latitude": lat}, options, 6)
    assert actual.absolute_origin == pytest.approx(absolute, abs=5e-9)
    assert actual.shift == pytest.approx(shift, abs=5e-9)
    assert actual.grid["x_m"] == pytest.approx(np.array(document["x_m"])-37, abs=5e-9)
    assert actual.grid["y_m"] == pytest.approx(np.array(document["y_m"])+19, abs=5e-9)
    assert actual.grid["source"]["origin_projected_m"] == pytest.approx(absolute, abs=5e-9)
    assert actual.grid["z_m"] == document["z_m"]
    assert actual.request["vessel_position_m"] == [0., 0., -2.]
    assert actual.request["anchor_position_m"] == pytest.approx([-9., 7., -20.], abs=5e-9)
    assert actual.request["initial_positions_m"] == pytest.approx(seed-np.array([37., -19., 0.]), abs=5e-9)
    assert actual.request["rest_lengths_m"] == rest
    assert actual.metadata()["vertical_translation_m"] == 0
    assert document == keep_document and options == keep_options


@pytest.mark.parametrize("crs", ["LOCAL_CARTESIAN_METRES", "EPSG:2277", "EPSG:4326"])
def test_real_plan_preparation_rejects_unbound_or_non_metric_geographic_frame(crs):
    c = configuration()
    c["seabed_grid"]["source"]["horizontal_crs"] = crs
    with pytest.raises(ValueError):
        prepare_plan_voyage(project(), c)


def test_equilibrium_preparation_maps_actual_plan_start_using_analytic_mercator_not_a_new_crs_label():
    p, c = project(), configuration()
    kept_p, kept_c = deepcopy(p), deepcopy(c)
    result = prepare_plan_voyage(p, c)
    m = result["mapping"]
    sim = result["config"]["simulation"]
    # The actual first plan instruction is an equatorial geodesic. Material
    # station20 determines its fraction without calling the bridge's _at.
    row = next(row for row in result["plan"]["instructions"] if row["cable_start_m"] <= 20 <= row["cable_end_m"] and row["cable_end_m"] > row["cable_start_m"])
    fraction = (20-row["cable_start_m"])/(row["cable_end_m"]-row["cable_start_m"])
    start_lon = row["vessel_start"][0]+fraction*(row["vessel_end"][0]-row["vessel_start"][0])
    absolute = mercator(start_lon, 0.)
    assert m["schema_version"] == 2
    assert m["origin_wgs84"] == pytest.approx([start_lon, 0.], abs=1e-12)
    assert m["terrain_frame"]["origin_projected_m"] == pytest.approx(absolute, abs=1e-9)
    assert m["terrain_frame"]["translation_from_original_local_m"] == pytest.approx(absolute, abs=1e-9)
    assert sim["seabed_grid"]["x_m"] == pytest.approx(np.array(c["seabed_grid"]["x_m"])-absolute[0], abs=1e-9)
    assert sim["seabed_grid"]["y_m"] == pytest.approx(c["seabed_grid"]["y_m"], abs=1e-9)
    assert sim["seabed_grid"]["z_m"] == c["seabed_grid"]["z_m"]
    assert sim["initial_equilibrium"]["anchor_position_m"] == pytest.approx([-absolute[0], 0., -10.], abs=1e-9)
    assert m["initial_natural_length_m"] == 20
    assert m["manufacturing_origin_m"] == 0
    assert m["initial_manufacturing_top_m"] == pytest.approx(20, abs=1e-12)
    assert p == kept_p and c == kept_c


def test_unequal_declared_rest_and_source_stock_survive_actual_dynamic_start_and_feed():
    c = configuration()
    del c["equilibrium_start"]["natural_length_m"]
    rest = [8., 1., 2., 1., 2., 1., 2., 1., 2.]
    c["equilibrium_start"]["rest_lengths_m"] = rest
    c["start_time_s"] = 60.
    # Derive an exact discrete, off-bed equilibrium independently. Each
    # segment carries bottom vertical force10 plus natural half-node wet
    # weights. Its chord follows Hooke's law; this is not a static/dynamic
    # initializer output and does not use the bridge's projection helper.
    rest_array = np.array(rest)
    mid_from_anchor = np.cumsum(rest_array[::-1])[::-1]-rest_array/2
    vertical = 10+4*mid_from_anchor
    def vectors(horizontal):
        tension = np.hypot(horizontal, vertical)
        stretch = rest_array*(1+tension/1e5)
        return np.column_stack([stretch*horizontal/tension, np.zeros(9), stretch*vertical/tension])
    horizontal = brentq(lambda h: vectors(h)[:, 2].sum()-10, 1, 1000)
    plan = build_ship_plan(project(), c["plan"])
    row = next(row for row in plan["instructions"] if row["time_s"] <= 60 <= row["time_s"]+row["duration_s"])
    fraction = (60-row["time_s"])/row["duration_s"]
    vessel_lon = row["vessel_start"][0]+fraction*(row["vessel_end"][0]-row["vessel_start"][0])
    seed = np.r_[np.zeros((1, 3)), -np.cumsum(vectors(horizontal), axis=0)]
    seed[:, :2] += mercator(vessel_lon, 0.)
    anchor_geo = geographic(*seed[-1, :2])
    c["equilibrium_start"]["anchor"] = {"longitude": anchor_geo[0], "latitude": anchor_geo[1], "z_model_m": -10.}
    c["equilibrium_start"]["initial_positions_m"] = seed.tolist()
    prepared = prepare_plan_voyage(project(), c)
    m = prepared["mapping"]
    assert m["initial_natural_length_m"] == sum(rest) == 20
    assert m["manufacturing_origin_m"] > 0
    assert m["initial_manufacturing_top_m"]-m["manufacturing_origin_m"] == pytest.approx(20, abs=1e-12)
    actual = run_voyage(project(), prepared["config"])
    first = actual["frames"][0]
    last = actual["frames"][-1]
    expected_feed = sum(row["manufacturing_end_m"]-row["manufacturing_start_m"] for row in m["instructions"])
    assert first["paid_out_m"] == 0
    assert first["material_length_m"] == pytest.approx(20, abs=1e-12)
    assert first["node_material_m"][0] == pytest.approx(m["initial_manufacturing_top_m"], abs=1e-10)
    assert first["node_material_m"][-1] == pytest.approx(m["manufacturing_origin_m"], abs=1e-10)
    assert last["paid_out_m"] == pytest.approx(expected_feed, abs=1e-10)
    assert last["material_length_m"] == pytest.approx(20+expected_feed, abs=1e-10)
    assert last["node_material_m"][0] == pytest.approx(m["final_manufacturing_top_m"], abs=1e-10)
    assert last["node_material_m"][-1] == pytest.approx(m["manufacturing_origin_m"], abs=1e-10)
    assert actual["checkpoint"]["physical_checkpoint"]["state"]["initialization_provenance"]["initial_snapshot"]["rest_lengths_m"] == rest
    assert first["touchdown"] is None and first["touchdown_detected"] is False
    assert first["nodes"][-1][2] == -10.


@pytest.mark.parametrize("mutation", ["crs", "origin", "vertical", "translation", "anchor", "stock_origin", "geographic_origin", "residual", "planned_target"])
def test_recomputed_checksum_cannot_make_inconsistent_frame_or_material_mapping_valid(mutation):
    m = deepcopy(prepare_plan_voyage(project(), configuration())["mapping"])
    if mutation == "crs": m["terrain_frame"]["horizontal_crs"] = "EPSG:32650"
    elif mutation == "origin": m["terrain_frame"]["origin_projected_m"] = [999., 888.]
    elif mutation == "vertical": m["terrain_frame"]["vertical_translation_m"] = 10.
    elif mutation == "translation": m["terrain_frame"]["translation_from_original_local_m"][0] += 10.
    elif mutation == "anchor": m["initial_anchor_xy_m"][0] += 1.
    elif mutation == "stock_origin": m["source_simulation"]["initial_suspended_material_m"] += 1.
    elif mutation == "geographic_origin": m["origin_wgs84"][0] += .01
    elif mutation == "residual": m["initial_target_touchdown_residual_m"] = 0.
    else:
        m["planned_start_touchdown_xy_m"][0] += 100.
        m["initial_target_touchdown_residual_m"] = math.dist(m["initial_anchor_xy_m"], m["planned_start_touchdown_xy_m"])
    m["checksum_sha256"] = checksum(m)
    with pytest.raises(ValueError):
        read_plan_mapping(m)


@pytest.mark.parametrize("rest", [["2"]*9, [10**1000]*9, [True]*9, [-2.]*9, []])
def test_malformed_saved_natural_segments_raise_valueerror_not_internal_numeric_errors(rest):
    m = deepcopy(prepare_plan_voyage(project(), configuration())["mapping"])
    request = m["source_simulation"]["initial_equilibrium"]
    request.pop("natural_length_m")
    request["rest_lengths_m"] = rest
    m["checksum_sha256"] = checksum(m)
    with pytest.raises(ValueError):
        read_plan_mapping(m)


@pytest.mark.parametrize("mutation", ["vessel_end", "final_stock", "payout_interval", "source_time"])
def test_saved_mapping_cannot_disagree_with_actual_controls_or_manufacturing_clock(mutation):
    m = deepcopy(prepare_plan_voyage(project(), configuration())["mapping"])
    if mutation == "vessel_end":
        m["instructions"][0]["vessel_end_xy_m"][0] += 100.
    elif mutation == "final_stock":
        m["final_manufacturing_top_m"] += 100.
    elif mutation == "payout_interval":
        m["instructions"][-1]["manufacturing_end_m"] += 1.
    else:
        m["source_plan_start_s"] = -100.
    m["checksum_sha256"] = checksum(m)
    with pytest.raises(ValueError):
        read_plan_mapping(m)


def test_actual_schema2_mapping_survives_resume_and_rejects_later_engineering_changes():
    p = project()
    prepared = prepare_plan_voyage(p, configuration(duration_s=.12))
    whole = run_voyage(p, prepared["config"])
    first = run_voyage(p, {**prepared["config"], "duration_s": .06})
    saved = json.loads(json.dumps(first["checkpoint"], allow_nan=False))
    read_voyage_checkpoint(saved)
    resumed = run_voyage({}, {"resume_state": saved, "duration_s": .06})
    assert resumed["plan_mapping"] == whole["plan_mapping"]
    for key in ["positions", "velocities", "rest_lengths_m", "node_material_m"]:
        assert resumed["checkpoint"]["physical_checkpoint"]["state"][key] == pytest.approx(np.array(whole["checkpoint"]["physical_checkpoint"]["state"][key]), abs=1e-8)
    changed = deepcopy(p)
    changed["route"]["points"][1]["depth_m"] = 12.
    with pytest.raises(ValueError, match="project no longer matches"):
        run_voyage(changed, {"resume_state": saved, "duration_s": .06})
    changed = deepcopy(p)
    changed["cable_types"][0]["ea_n"] += 1000.
    with pytest.raises(ValueError, match="project no longer matches"):
        run_voyage(changed, prepared["config"])


def test_http_bad_saved_schema2_material_is_422_and_real_preparation_can_run(tmp_path):
    prepared = prepare_plan_voyage(project(), configuration())
    c = deepcopy(prepared["config"])
    request = c["plan_mapping"]["source_simulation"]["initial_equilibrium"]
    request.pop("natural_length_m")
    request["rest_lengths_m"] = ["2"]*9
    c["simulation"] = deepcopy(c["plan_mapping"]["source_simulation"])
    c["plan_mapping"]["checksum_sha256"] = checksum(c["plan_mapping"])
    with TestClient(create_app(ProjectStore(tmp_path/"plan-review.sqlite3")), raise_server_exceptions=False) as client:
        response = client.post("/api/voyage/run", json={"project": project(), "config": c})
        assert response.status_code == 422, response.text
        response = client.post("/api/shipplan/prepare-voyage", json={"project": project(), "config": configuration()})
        assert response.status_code == 200, response.text
        actual = client.post("/api/voyage/run", json={"project": project(), "config": response.json()["config"]})
        assert actual.status_code == 200, actual.text
        assert actual.json()["frames"][0]["touchdown"] is None
