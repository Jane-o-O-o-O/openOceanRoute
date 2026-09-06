"""True geographic current initialization, using an independent traction BVP.

Only route/source input construction is shared with the previous test fixture;
the nonzero-current reference uses its own material intersections and force
recurrence. Projected x/y, initial inventory, flow proof and drive/resume are
checked through the actual public geographic bridge and HTTP endpoints.
"""
from copy import deepcopy
import json
import math

import numpy as np
import pytest
from scipy.optimize import root
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.plan_voyage import prepare_plan_voyage, read_plan_mapping
from oceanroute.shipplan import build_ship_plan
from oceanroute.storage import ProjectStore
from oceanroute.voyage import run_voyage
from test_heterogeneous_plan_voyage import fixture as still_fixture, RADIUS, checksum


def fixture(shear=False):
    project, config, state, absolute, shift = still_fixture(-50.)
    rest, q, alpha = state["rest"], state["coordinates"], state["alpha"]
    first = np.array([max(0., min(high, 10.2)-max(low, 0.))
                      for high, low in zip(q[:-1], q[1:])])
    segment_cd_d = 1.2*(.02*first+.03*(rest-first))
    kc = 1025./2*np.r_[segment_cd_d[0]/2,
                      (segment_cd_d[:-1]+segment_cd_d[1:])/2, segment_cd_d[-1]/2]
    kb = 1025./2*.1*1.2*alpha
    uniform = [.32, -.27, 0.]
    profile = [{"depth_m": 0., "x_m_s": .2, "y_m_s": -.15},
               {"depth_m": 40., "x_m_s": .55, "y_m_s": .25}] if shear else None
    bottom = np.array([150., 10., 15.])

    def geometry(x):
        force = np.vstack([x.reshape(-1, 3), bottom])
        tension = np.linalg.norm(force, axis=1)
        vectors = (rest*(1+tension/state["ea"]))[:, None]*force/tension[:, None]
        return np.vstack([[0., 0., -2.], [0., 0., -2.]-np.cumsum(vectors, axis=0)])

    def loads(p):
        secant = np.vstack([p[1]-p[0], (p[2:]-p[:-2])/2, p[-1]-p[-2]])
        tangent = secant/np.linalg.norm(secant, axis=1)[:, None]
        u = np.tile(uniform, (len(p), 1))
        if profile:
            fraction = np.clip(-p[:, 2]/40., 0., 1.)
            u = np.column_stack([.2+.35*fraction, -.15+.4*fraction, np.zeros(len(p))])
        normal = u-np.sum(u*tangent, axis=1)[:, None]*tangent
        cable = kc[:, None]*np.linalg.norm(normal, axis=1)[:, None]*normal
        body = kb[:, None]*np.linalg.norm(u, axis=1)[:, None]*u
        force = cable+body
        force[:, 2] -= state["weight"]
        return force, cable, body, u

    def balance(x):
        force = np.vstack([x.reshape(-1, 3), bottom])
        return (force[:-1]-force[1:]+loads(geometry(x))[0][1:-1]).ravel()

    seed = np.column_stack([np.full(len(rest), 150.), np.full(len(rest), 10.), state["vertical"]])
    solved = root(balance, seed[:-1].ravel(), tol=1e-11)
    assert solved.success and np.max(np.abs(balance(solved.x))) < 1e-9
    positions = geometry(solved.x)
    _, cable, body, u = loads(positions)
    assert np.max(np.abs(cable[:, 2])) > .1
    config["simulation"].update({"current_x_m_s": uniform[0], "current_y_m_s": uniform[1],
                                "water_density_kg_m3": 1025., "damping_ratio": 0.})
    if profile:
        config["simulation"].update({"current_x_m_s": 1.7, "current_y_m_s": -1.3,
                                    "current_profile": profile})
    # The commanded ship setback also responds to the declared uniform plan
    # current. Locate material31 on that actual command, then convert both
    # geographic coordinates with the explicit Mercator formula.
    command = build_ship_plan(project, {**config["plan"], **{
        key: config["simulation"][key] for key in ("current_x_m_s", "current_y_m_s", "water_density_kg_m3")}})
    row = next(r for r in command["instructions"] if r["cable_start_m"] < 31 < r["cable_end_m"])
    fraction = (31-row["cable_start_m"])/(row["cable_end_m"]-row["cable_start_m"])
    config["start_time_s"] = row["time_s"]+fraction*row["duration_s"]
    lon, lat = [(1-fraction)*row["vessel_start"][i]+fraction*row["vessel_end"][i] for i in (0, 1)]
    absolute = np.array([RADIUS*math.radians(lon), RADIUS*math.asinh(math.tan(math.radians(lat)))])
    shift = absolute-np.array([11., -7.])
    anchor = positions[-1]+np.r_[absolute, 0.]
    eq = config["equilibrium_start"]
    eq.update({"schema": "oceanroute.dynamic.initial-equilibrium.v2",
               "anchor": {"longitude": math.degrees(anchor[0]/RADIUS),
                          "latitude": math.degrees(math.atan(math.sinh(anchor[1]/RADIUS))),
                          "z_model_m": anchor[2]},
               "initial_positions_m": (positions+np.r_[shift, 0.]).tolist(),
               "solver": {"force_tolerance_n": 1e-6, "relative_force_tolerance": 1e-9,
                          "contact_tolerance_m": 1e-8}})
    state.update({"positions": positions, "cable_drag": cable, "body_drag": body,
                  "fluid": u, "kc": kc, "kb": kb})
    return project, config, state, absolute, shift


@pytest.mark.parametrize("shear", [False, True], ids=["uniform", "depth-shear"])
def test_geographic_true_current_proof_and_real_drive_resume(shear, monkeypatch):
    project, config, expected, absolute, shift = fixture(shear)
    original = deepcopy((project, config))
    prepared = prepare_plan_voyage(project, config)
    proof = prepared["mapping"]["initial_equilibrium_preparation"]["provenance"]
    assert proof["schema"] == "oceanroute.dynamic.initial-equilibrium.provenance.v3"
    snapshot = proof["initial_snapshot"]
    assert np.asarray(snapshot["positions"]) == pytest.approx(expected["positions"], abs=1e-8)
    assert snapshot["node_material_m"] == pytest.approx(expected["coordinates"], abs=1e-12)
    assert snapshot["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-10)
    loading = proof["fluid_loading"]
    for key, expected_key in (("node_cable_drag_n", "cable_drag"), ("node_body_drag_n", "body_drag"),
                              ("node_fluid_velocity_m_s", "fluid")):
        assert np.asarray(loading[key]) == pytest.approx(expected[expected_key], abs=1e-8)
    frame = prepared["mapping"]["terrain_frame"]
    assert frame["origin_projected_m"] == pytest.approx(absolute, abs=1e-9)
    assert frame["translation_from_original_local_m"] == pytest.approx(shift, abs=1e-9)
    assert prepared["mapping"]["manufacturing_origin_m"] == pytest.approx(4., abs=1e-12)
    assert (project, config) == original
    read_plan_mapping(json.loads(json.dumps(prepared["mapping"])), simulation=prepared["config"]["simulation"])
    whole = run_voyage(project, prepared["config"])
    first = run_voyage(project, {**prepared["config"], "duration_s": .04})
    saved = json.loads(json.dumps(first["checkpoint"], allow_nan=False))
    import oceanroute.initial_equilibrium as initialization
    monkeypatch.setattr(initialization, "resolve_initial_equilibrium",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resume invoked optimizer")))
    continued = run_voyage({}, {"resume_state": saved, "duration_s": .04})
    physical = whole["checkpoint"]["physical_checkpoint"]
    restored = continued["checkpoint"]["physical_checkpoint"]
    assert physical["schema_version"] == restored["schema_version"] == 4
    assert physical["state"]["initialization_provenance"] == restored["state"]["initialization_provenance"] == proof
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_wet_weight_n"):
        assert np.asarray(restored["state"][key]) == pytest.approx(np.asarray(physical["state"][key]), abs=1e-9)
    assert physical["state"]["paid_out_m"] == pytest.approx(.51*.08, abs=1e-12)
    assert whole["frames"][0]["touchdown"] is None
    assert np.linalg.norm(np.asarray(physical["state"]["positions"])[0]-expected["positions"][0]) > .01


def test_resigned_geographic_raw_flow_mismatch_rejected():
    project, config, _, _, _ = fixture(True)
    mapping = prepare_plan_voyage(project, config)["mapping"]
    changed = deepcopy(mapping)
    changed["source_simulation"]["initial_equilibrium"]["initial_fluid"]["current_profile"][0]["x_m_s"] += .01
    changed["checksum_sha256"] = checksum(changed)
    with pytest.raises(ValueError):
        read_plan_mapping(changed)


def test_actual_http_geographic_current_preview_run_and_json_continue(tmp_path):
    project, config, expected, _, _ = fixture(True)
    with TestClient(create_app(ProjectStore(tmp_path/"current-geographic.sqlite3")), raise_server_exceptions=False) as client:
        response = client.post("/api/shipplan/prepare-voyage", json={"project": project, "config": config})
        assert response.status_code == 200, response.text
        prepared = response.json()
        first_config = {**prepared["config"], "duration_s": .04}
        first = client.post("/api/voyage/run", json={"project": project, "config": first_config})
        assert first.status_code == 200, first.text
        assert np.asarray(first.json()["frames"][0]["node_cable_drag_force_n"]) == pytest.approx(expected["cable_drag"], abs=1e-8)
        continuation = client.post("/api/voyage/run", json={"project": {}, "config": {
            "resume_state": first.json()["checkpoint"], "duration_s": .04}})
        assert continuation.status_code == 200, continuation.text
        assert continuation.json()["checkpoint"]["physical_checkpoint"]["schema_version"] == 4


@pytest.mark.parametrize("schema", ["oceanroute.dynamic.initial-equilibrium.v3", None, 2, ""])
def test_geographic_current_requires_explicit_supported_schema(schema):
    project, config, _, _, _ = fixture()
    config["equilibrium_start"]["schema"] = schema
    with pytest.raises(ValueError):
        prepare_plan_voyage(project, config)
