"""Independent material/force checks for explicit equilibrium initial states.

Expected cable geometry is a closed discrete force balance, not output from
the static or dynamic model's internal geometry/material helpers. All dynamic,
HTTP, checkpoint and durable-job paths use the actual implementation.
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
from oceanroute.checkpoints import read_checkpoint
from oceanroute.simulation import simulate_lay
from oceanroute.storage import ProjectStore


GRAVITY = 9.80665
INITIAL_SCHEMA = "oceanroute.dynamic.initial-equilibrium.v1"


def bed(curved=False):
    axis = [-100., 0., 100.]
    return {"schema": "oceanroute.bathymetry.v1", "x_m": axis, "y_m": axis,
            "z_m": [[-40.+.1*x+.05*y+.0003*x*y if curved else -100.
                     for x in axis] for y in axis],
            "source": {"name": "explicit independent review bed",
                       "horizontal_crs": "LOCAL_CARTESIAN_METRES",
                       "origin_projected_m": [0, 0], "vertical_datum": "model sea zero"}}


def closed_state():
    """Exact discrete tension-only equilibrium, with unequal natural segments.

    At each free node V_left-V_right equals the two adjacent half weights;
    horizontal forces cancel. Hooke's law fixes every stretched segment.
    The first rest length exceeds twice the mean, detecting silent remeshing.
    """
    rest = np.array([12., 2., 3., 1., 4., 2., 3.])
    weight, ea, horizontal, bottom_vertical = 4., 10000., 150., 15.
    mid = np.array([rest[i+1:].sum()+rest[i]/2 for i in range(len(rest))])
    vertical = bottom_vertical+weight*mid
    tension = np.hypot(horizontal, vertical)
    direction = np.column_stack((horizontal/tension, np.zeros(len(rest)), vertical/tension))
    vectors = rest[:, None]*(1+tension[:, None]/ea)*direction
    vessel = np.array([3., -4., -2.])
    positions = np.vstack((vessel, vessel-np.cumsum(vectors, axis=0)))
    return positions, rest, tension


def configuration(**changes):
    positions, rest, _ = closed_state()
    c = {"seabed_grid": bed(), "nodes": len(positions), "wet_weight_n_m": 4.,
         "ea_n": 10000., "ei_n_m2": 0., "diameter_m": .02, "mass_kg_m": 1.2,
         "water_density_kg_m3": 1025., "added_mass_coefficient": .7,
         "ship_speed_m_s": 0., "payout_m_s": 0., "seabed_friction": .6,
         "current_x_m_s": 0., "current_y_m_s": 0., "damping_ratio": 0.,
         "duration_s": .04, "dt_s": .02, "internal_dt_s": .002,
         "solver_iterations": 32, "initial_suspended_material_m": 37.,
         "checkpoint_times_s": [0.],
         "initial_equilibrium": {"schema": INITIAL_SCHEMA,
             "vessel_position_m": positions[0].tolist(),
             "anchor_position_m": positions[-1].tolist(),
             "rest_lengths_m": rest.tolist(), "initial_positions_m": positions.tolist()}}
    c.update(changes)
    return c


def nodal_halves(rest):
    return np.r_[rest[0]/2, (rest[:-1]+rest[1:])/2, rest[-1]/2]


def independent_forces(positions, rest, ea=10000., weight=4.):
    delta = np.diff(positions, axis=0)
    length = np.linalg.norm(delta, axis=1)
    tension = ea*np.maximum(length/rest-1, 0.)
    segment = tension[:, None]*delta/length[:, None]
    internal = np.zeros_like(positions)
    internal[:-1] += segment
    internal[1:] -= segment
    external = np.zeros_like(positions)
    external[:, 2] = -weight*nodal_halves(rest)
    return tension, internal, external


def checksum(document):
    """Independent JSON checksum only; it is not an expected physical state."""
    def canonical(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value
    payload = {key: value for key, value in document.items() if key != "checksum_sha256"}
    return hashlib.sha256(json.dumps(canonical(payload), sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def test_closed_nonuniform_equilibrium_preserves_natural_material_and_actual_mass():
    expected, rest, tension = closed_state()
    result = simulate_lay({}, configuration())
    first = result["frames"][0]
    assert first["nodes"] == pytest.approx(expected, abs=1e-10)
    assert first["ship"] == pytest.approx([3., -4., -2.], abs=1e-12)
    assert first["node_velocity_m_s"] == pytest.approx(np.zeros_like(expected), abs=0)
    assert first["paid_out_m"] == 0
    assert first["material_length_m"] == pytest.approx(27., abs=1e-13)
    assert first["geometric_length_m"] > first["material_length_m"]
    coordinates = 37.+np.r_[np.cumsum(rest[::-1])[::-1], 0.]
    assert first["node_material_m"] == pytest.approx(coordinates, abs=1e-12)
    assert first["top_tension_n"] == pytest.approx(tension[0], abs=1e-8)
    assert first["anchor_segment_tension_n"] == pytest.approx(tension[-1], abs=1e-8)
    assert first["touchdown_detected"] is False
    assert first["touchdown"] is None and first["touchdown_node_index"] is None
    assert first["bottom_tension_n"] is None
    half = nodal_halves(rest)
    assert first["node_wet_weight_n"] == pytest.approx(4*half, abs=1e-11)
    assert first["node_dry_mass_kg"] == pytest.approx(1.2*half, abs=1e-11)
    effective_mass_per_m = 1.2+.7*1025*math.pi*.02**2/4
    assert first["node_mass_kg"] == pytest.approx(effective_mass_per_m*half, abs=1e-11)
    assert result["checkpoint"]["state"]["rest_lengths_m"] == pytest.approx(rest, abs=0)
    assert len(result["frames"][-1]["nodes"]) == len(expected), "zero payout must not remesh an unequal initial top element"
    assert result["frames"][-1]["ship"][2] == -2., "no heave must preserve the actual submerged fairlead"
    actual_t, internal, external = independent_forces(np.array(first["nodes"]), rest)
    assert actual_t == pytest.approx(tension, abs=1e-8)
    assert np.max(np.linalg.norm((internal+external)[1:-1], axis=1)) < 1e-8
    vessel_support = -internal[0]-external[0]
    assert vessel_support[2]-tension[0]*(expected[0, 2]-expected[1, 2])/np.linalg.norm(expected[0]-expected[1]) == pytest.approx(24., abs=1e-9)


def test_curved_bed_uses_complete_static_geometry_and_preserves_80m_inventory():
    c = configuration(seabed_grid=bed(True), nodes=18, duration_s=.02)
    c["initial_equilibrium"] = {"schema": INITIAL_SCHEMA,
        "vessel_position_m": [0., 0., 0.], "anchor_position_m": [-60., -10., -46.32],
        "natural_length_m": 80.}
    r = simulate_lay({}, c)
    f = r["frames"][0]
    p = np.asarray(f["nodes"])
    rest = np.full(17, 80/17)
    _, internal, external = independent_forces(p, rest)
    z = -40+.1*p[:, 0]+.05*p[:, 1]+.0003*p[:, 0]*p[:, 1]
    gradient = np.column_stack((.1+.0003*p[:, 1], .05+.0003*p[:, 0]))
    normals = np.column_stack((-gradient, np.ones(len(p))))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    assert f["material_length_m"] == pytest.approx(80, abs=1e-12)
    assert f["geometric_length_m"] > 81
    assert p[-1] == pytest.approx([-60., -10., -46.32], abs=1e-10)
    assert f["node_seabed_normal"] == pytest.approx(normals, abs=1e-12)
    assert np.min(p[:, 2]-z) >= -1e-8
    force = internal+external
    contact = p[:, 2]-z <= 1e-8
    normal_support = np.where(contact, np.maximum(-np.sum(force*normals, axis=1), 0), 0)
    residual = force+normal_support[:, None]*normals
    assert np.max(np.linalg.norm(residual[1:-1], axis=1)) < .01
    for a, b in zip(p[:-1], p[1:]):
        points = a+np.linspace(0, 1, 501)[:, None]*(b-a)
        floor = -40+.1*points[:, 0]+.05*points[:, 1]+.0003*points[:, 0]*points[:, 1]
        assert np.min(points[:, 2]-floor) >= -1e-8
    assert r["checkpoint"]["state"]["initial_material_length_m"] == pytest.approx(80, abs=1e-12)
    assert r["checkpoint"]["state"]["paid_out_m"] == 0


def test_zero_actuation_holds_exact_discrete_equilibrium_without_startup_settling():
    p, _, _ = closed_state()
    a = simulate_lay({}, configuration(duration_s=.1, internal_dt_s=.004))
    b = simulate_lay({}, configuration(duration_s=.1, internal_dt_s=.002))
    for r in [a, b]:
        actual = np.array(r["frames"][-1]["nodes"])
        assert np.max(np.linalg.norm(actual-p, axis=1)) < 1e-7
        assert np.max(np.linalg.norm(np.asarray(r["frames"][-1]["node_velocity_m_s"]), axis=1)) < 1e-6
    assert a["frames"][-1]["nodes"] == pytest.approx(np.asarray(b["frames"][-1]["nodes"]), abs=1e-7)


def test_first_payout_advances_only_top_manufacturing_station_not_initial_stock():
    c = configuration(payout_m_s=.25, duration_s=.08)
    r = simulate_lay({}, c)
    s = r["checkpoint"]["state"]
    assert s["initial_material_length_m"] == pytest.approx(27., abs=1e-13)
    assert s["paid_out_m"] == pytest.approx(.02, abs=1e-13)
    assert sum(s["rest_lengths_m"]) == pytest.approx(27.02, abs=1e-12)
    assert s["node_material_m"][0] == pytest.approx(64.02, abs=1e-12)
    assert s["node_material_m"][-1] == 37.


def test_prescribed_actuation_moves_real_cable_and_feed_without_pinning_static_shape():
    initial, _, _ = closed_state()
    result = simulate_lay({}, configuration(ship_speed_m_s=.2, payout_m_s=.25, duration_s=.08))
    last = result["frames"][-1]
    assert last["ship"] == pytest.approx([3.016, -4., -2.], abs=1e-12)
    assert last["nodes"][-1] == pytest.approx(initial[-1], abs=1e-12)
    assert np.max(np.linalg.norm(np.asarray(last["nodes"])[1:-1]-initial[1:-1], axis=1)) > 1e-7
    assert last["paid_out_m"] == pytest.approx(.02, abs=1e-13)
    assert sum(last["node_dry_mass_kg"]) == pytest.approx(1.2*27.02, abs=1e-11)
    assert sum(last["node_wet_weight_n"]) == pytest.approx(4*27.02, abs=1e-11)


def test_exact_resume_uses_saved_material_and_provenance_without_second_initialization():
    c = configuration(duration_s=.08, payout_m_s=.25)
    full = simulate_lay({}, c)
    first = simulate_lay({}, {**c, "duration_s": .04})
    cp = json.loads(json.dumps(first["checkpoint"], allow_nan=False))
    assert cp["schema_version"] == 3
    assert cp["state"]["initialization_provenance"]
    read_checkpoint(cp)
    resumed = simulate_lay({}, {"resume_state": cp, "duration_s": .04})
    for key in ["positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg"]:
        assert resumed["checkpoint"]["state"][key] == pytest.approx(np.asarray(full["checkpoint"]["state"][key]), abs=1e-9)
    assert resumed["checkpoint"]["state"]["paid_out_m"] == pytest.approx(.02, abs=1e-13)
    assert resumed["checkpoint"]["state"]["initialization_provenance"] == cp["state"]["initialization_provenance"]
    with pytest.raises(ValueError):
        simulate_lay({}, {"resume_state": cp, "duration_s": .02,
            "initial_equilibrium": {**c["initial_equilibrium"], "natural_length_m": 40.}})


@pytest.mark.parametrize("change", [
    {"ei_n_m2": 1.}, {"current_x_m_s": .1},
    {"inline_bodies": [{"id": "initial-point", "material_m": 40., "mass_kg": 1., "wet_weight_n": 2.}]},
    {"material_segments": [{"start_m": 0., "end_m": 50., "wet_weight_n_m": 4., "ea_n": 10000.},
                            {"start_m": 50., "end_m": 100., "wet_weight_n_m": 5., "ea_n": 10000.}]},
])
def test_incompatible_initial_physics_is_rejected_instead_of_faking_static_equilibrium(change):
    with pytest.raises(ValueError):
        simulate_lay({}, configuration(**change))


def test_accepted_json_is_not_a_valid_raw_initializer_and_failed_budget_does_not_fallback():
    c = configuration()
    c["initial_equilibrium"]["accepted"] = True
    with pytest.raises(ValueError):
        simulate_lay({}, c)
    c = configuration()
    c["initial_equilibrium"]["solver"] = {"max_work_units": 1}
    with pytest.raises(ValueError):
        simulate_lay({}, c)


def test_actual_unaccepted_static_solve_does_not_start_approximate_dynamics():
    c = configuration(seabed_grid=bed(True), nodes=18)
    c["initial_equilibrium"] = {"schema": INITIAL_SCHEMA,
        "vessel_position_m": [0., 0., 0.], "anchor_position_m": [-60., -10., -46.32],
        "natural_length_m": 80., "solver": {"max_solver_iterations": 1}}
    with pytest.raises(ValueError, match="not accepted"):
        simulate_lay({}, c)


def test_checkpoint_proof_edit_even_with_recomputed_checksum_is_rejected():
    r = simulate_lay({}, configuration())
    cp = deepcopy(r["checkpoint"])
    cp["config"]["initial_equilibrium"]["anchor_position_m"][0] += .5
    cp["checksum_sha256"] = checksum(cp)
    with pytest.raises(ValueError):
        read_checkpoint(cp)


@pytest.mark.parametrize("field,value", [
    ("max_node_force_residual_n", 1000000.),
    ("total_force_balance_n", [1000000., 0., 0.]),
])
def test_checkpoint_cannot_publish_false_force_evidence_after_checksum_recalculation(field, value):
    cp = deepcopy(simulate_lay({}, configuration())["checkpoint"])
    cp["state"]["initialization_provenance"]["verification"][field] = value
    cp["checksum_sha256"] = checksum(cp)
    with pytest.raises(ValueError):
        read_checkpoint(cp)


@pytest.mark.parametrize("mutation", ["nodata", "two_lengths", "wrong_nodes"])
def test_bad_initial_geometry_contract_rejects_without_approximate_fallback(mutation):
    c = configuration()
    if mutation == "nodata":
        c["seabed_grid"]["z_m"][0][0] = None
    elif mutation == "two_lengths":
        c["initial_equilibrium"]["natural_length_m"] = 27.
    else:
        c["nodes"] = 7
    with pytest.raises(ValueError):
        simulate_lay({}, c)


def terminal(client, identifier):
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        response = client.get(f"/api/voyage/jobs/{identifier}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] not in {"queued", "running", "cancelling"}:
            return value
        time.sleep(.01)
    pytest.fail("real equilibrium-initialized job did not finish")


def test_actual_http_and_durable_job_reopen_resume_preserve_initial_inventory(tmp_path):
    database = tmp_path/"isolated.sqlite3"
    c = configuration(duration_s=.04, payout_m_s=.25)
    with TestClient(create_app(ProjectStore(database))) as client:
        response = client.post("/api/simulation/dynamic", json={"project": {}, "config": c})
        assert response.status_code == 200, response.text
        assert response.json()["checkpoint"]["schema_version"] == 3
        bad = deepcopy(c)
        bad["initial_equilibrium"]["accepted"] = True
        assert client.post("/api/simulation/dynamic", json={"project": {}, "config": bad}).status_code == 422
        c.pop("checkpoint_times_s")
        response = client.post("/api/voyage/jobs", json={"project": {}, "config": {
            "duration_s": .04, "chunk_duration_s": .02, "simulation": c,
            "adaptive_mesh": {"enabled": False}, "max_total_work_units": 120000000}})
        assert response.status_code == 200, response.text
        identifier = response.json()["id"]
        status = terminal(client, identifier)
        assert status["status"] == "completed", status
        checkpoint = client.get(f"/api/voyage/jobs/{identifier}/checkpoint").json()
    with TestClient(create_app(ProjectStore(database))) as client:
        saved = client.get(f"/api/voyage/jobs/{identifier}/checkpoint")
        assert saved.status_code == 200, saved.text
        assert saved.json() == checkpoint
        response = client.post(f"/api/voyage/jobs/{identifier}/resume", json={"duration_s": .04})
        assert response.status_code == 200, response.text
        child = response.json()["id"]
        status = terminal(client, child)
        assert status["status"] == "completed", status
        resumed = client.get(f"/api/voyage/jobs/{child}/checkpoint").json()
        state = resumed["physical_checkpoint"]["state"]
        assert state["initial_material_length_m"] == pytest.approx(27., abs=1e-12)
        assert state["paid_out_m"] == pytest.approx(.02, abs=1e-12)
        assert state["node_material_m"][0] == pytest.approx(64.02, abs=1e-11)
        assert resumed["physical_checkpoint"]["config"]["seabed_grid"] == c["seabed_grid"]
        assert state["initialization_provenance"] == checkpoint["physical_checkpoint"]["state"]["initialization_provenance"]
