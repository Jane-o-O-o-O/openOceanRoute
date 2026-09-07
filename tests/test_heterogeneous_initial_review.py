"""Independent mathematical/material audit of heterogeneous static startup.

Declared material intervals are integrated with Decimal local overlaps. Exact
off-bed geometry is constructed from nodal force balance and Hooke compliance,
never from a static/dynamic geometry, loading or initialization helper. Point
loads use the declared natural station's virtual work, not display positions.
The private material operator is tested only as a subject of integration tests;
it never supplies expected loads or the analytical seed.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
import math
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from oceanroute.api import create_app
from oceanroute.checkpoints import read_checkpoint
from oceanroute.initial_equilibrium import resolve_initial_equilibrium
from oceanroute.simulation import _MaterialModel, _environment, simulate_lay
from oceanroute.storage import ProjectStore
from oceanroute.voyage import read_voyage_checkpoint, run_voyage


G = 9.80665
REST = [12., 2., 3., 1., 4., 2., 3.]
ORIGIN = 37.
PROVENANCE_V2 = "oceanroute.dynamic.initial-equilibrium.provenance.v2"
OPERATOR = "natural-half-segment-cable-and-material-linear-point-load-v1"


def decimal(value):
    """Exact binary JSON coordinate/length value, with no large-prefix loss."""
    return Decimal.from_float(float(value))


def half_totals(values):
    values = np.asarray(values, dtype=float)
    return np.r_[values[0]/2, (values[:-1]+values[1:])/2, values[-1]/2]


def declared_loading(c, rest):
    """Integrate each interval locally in 60-digit arithmetic.

    This is an interval-intersection oracle, not cumulative-prefix subtraction
    or the production material search. Body point forces follow B*z_body's
    virtual work in the prescribed natural element.
    """
    with localcontext() as context:
        context.prec = 60
        lengths = [decimal(v) for v in rest]
        origin = decimal(c["initial_suspended_material_m"])
        coordinates = [origin+sum(lengths[j:], Decimal(0)) for j in range(len(rest)+1)]
        totals = {name: [] for name in ("wet", "dry", "displaced", "compliance")}
        for low, high in zip(coordinates[1:], coordinates[:-1]):
            per_segment = {name: Decimal(0) for name in totals}
            covered = Decimal(0)
            for row in c["material_segments"]:
                overlap = max(Decimal(0), min(high, decimal(row["end_m"]))-max(low, decimal(row["start_m"])))
                covered += overlap
                per_segment["wet"] += overlap*decimal(row["wet_weight_n_m"])
                per_segment["dry"] += overlap*decimal(row["mass_kg_m"])
                displaced = c["water_density_kg_m3"]*math.pi*row["diameter_m"]**2/4
                per_segment["displaced"] += overlap*decimal(displaced)
                per_segment["compliance"] += overlap/decimal(row["ea_n"])
            assert covered == high-low, "the independent fixture must have complete exact coverage"
            for name in totals:
                totals[name].append(float(per_segment[name]))
        alpha = {}
        body_wet = np.zeros(len(rest)+1)
        body_dry = np.zeros(len(rest)+1)
        body_mass = np.zeros(len(rest)+1)
        deployed_absolute = []
        for body in c.get("inline_bodies", []):
            assert body.get("length_m", 0) == 0, "point-load oracle does not approximate rods"
            station = decimal(body["material_m"])
            fractions = np.zeros(len(rest)+1)
            if coordinates[-1] <= station <= coordinates[0]:
                for i in range(len(rest)):
                    if coordinates[i+1] <= station <= coordinates[i]:
                        fraction = float((station-coordinates[i+1])/lengths[i])
                        fractions[i:i+2] = [fraction, 1-fraction]
                        break
            alpha[body["id"]] = fractions
            body_wet += fractions*body["wet_weight_n"]
            body_dry += fractions*body["mass_kg"]
            body_mass += fractions*(body["mass_kg"]+c["added_mass_coefficient"]*max(0., body["mass_kg"]-body["wet_weight_n"]/G))
            deployed_absolute.append(float(fractions.sum())*abs(body["wet_weight_n"]))
    cable_wet = half_totals(totals["wet"])
    cable_dry = half_totals(totals["dry"])
    cable_mass = half_totals(np.asarray(totals["dry"])+c["added_mass_coefficient"]*np.asarray(totals["displaced"]))
    return {"coordinates": np.array([float(q) for q in coordinates]),
            "segment_wet": np.asarray(totals["wet"]), "compliance": np.asarray(totals["compliance"]),
            "ea": np.asarray(rest)/np.asarray(totals["compliance"]),
            "cable_wet": cable_wet, "body_wet": body_wet, "weight": cable_wet+body_wet,
            "dry": cable_dry+body_dry, "mass": cable_mass+body_mass, "alpha": alpha,
            "absolute_load_scale_n": math.fsum(totals["wet"]+deployed_absolute)}


def explicit_bed():
    return {"schema": "oceanroute.bathymetry.v1", "x_m": [-100., 0., 100.], "y_m": [-100., 0., 100.],
            "z_m": [[-100., -100., -100.] for _ in range(3)],
            "source": {"name": "explicit synthetic independent heterogeneous review bed; not survey data",
                       "horizontal_crs": "LOCAL_CARTESIAN_METRES", "origin_projected_m": [0., 0.],
                       "vertical_datum": "synthetic model sea zero"}}


def configuration(*, bodies=None, rotation=0., **changes):
    """Build a known exact discrete equilibrium from declared interval loads."""
    rows = [{"id": "lower", "start_m": 0., "end_m": 40.5, "wet_weight_n_m": 2.5,
             "ea_n": 8000., "mass_kg_m": .9, "diameter_m": .018, "ei_n_m2": 0.},
            {"id": "middle", "start_m": 40.5, "end_m": 50.75, "wet_weight_n_m": 5.,
             "ea_n": 14000., "mass_kg_m": 1.4, "diameter_m": .025, "ei_n_m2": 0.},
            {"id": "upper", "start_m": 50.75, "end_m": 80., "wet_weight_n_m": 3.,
             "ea_n": 45000., "mass_kg_m": 1.2, "diameter_m": .02, "ei_n_m2": 0.},
            {"id": "future", "start_m": 80., "end_m": 100., "wet_weight_n_m": 6.,
             "ea_n": 21000., "mass_kg_m": 1.5, "diameter_m": .03, "ei_n_m2": 0.}]
    if bodies is None:
        bodies = [{"id": "anchor-load", "material_m": 37., "mass_kg": 1., "wet_weight_n": 3., "length_m": 0.},
                  {"id": "buoyant-inside", "material_m": 39.25, "mass_kg": .4, "wet_weight_n": -25., "length_m": 0.},
                  {"id": "at-node", "material_m": 46., "mass_kg": .6, "wet_weight_n": 2., "length_m": 0.},
                  {"id": "inside-upper", "material_m": 58.75, "mass_kg": .8, "wet_weight_n": 4., "length_m": 0.},
                  {"id": "vessel-load", "material_m": 64., "mass_kg": 2., "wet_weight_n": 1., "length_m": 0.},
                  {"id": "future-point", "material_m": 64.01, "mass_kg": 1., "wet_weight_n": -2., "length_m": 0.}]
    c = {"seabed_grid": explicit_bed(), "nodes": 8, "wet_weight_n_m": 4., "ea_n": 10000.,
         "ei_n_m2": 0., "diameter_m": .02, "mass_kg_m": 1.2, "water_density_kg_m3": 1025.,
         "added_mass_coefficient": .7, "drag_coefficient": 1.2, "initial_suspended_material_m": ORIGIN,
         "ship_speed_m_s": 0., "payout_m_s": 0., "current_x_m_s": 0., "current_y_m_s": 0.,
         "heading_deg": 90., "seabed_friction": .6, "damping_ratio": 0.,
         "duration_s": .04, "dt_s": .02, "internal_dt_s": .002, "solver_iterations": 32,
         "material_segments": rows, "inline_bodies": deepcopy(bodies), "checkpoint_times_s": [0.]}
    c.update(changes)
    loading = declared_loading(c, REST)
    horizontal = 150.
    vertical = np.empty(7)
    vertical[-1] = 35.
    for i in range(5, -1, -1):
        vertical[i] = vertical[i+1]+loading["weight"][i+1]
    tension = np.hypot(horizontal, vertical)
    rotation_matrix = np.array([[math.cos(rotation), -math.sin(rotation), 0.],
                                [math.sin(rotation), math.cos(rotation), 0.], [0., 0., 1.]])
    toward_ship = np.column_stack([np.full(7, horizontal), np.zeros(7), vertical])/tension[:, None]
    vectors = (np.asarray(REST)+tension*loading["compliance"])[:, None]*toward_ship
    vessel = np.array([3., -4., -2.])
    positions = np.vstack([vessel, vessel-np.cumsum(vectors, axis=0)])@rotation_matrix.T
    c["initial_equilibrium"] = {"schema": "oceanroute.dynamic.initial-equilibrium.v1",
        "vessel_position_m": positions[0].tolist(), "anchor_position_m": positions[-1].tolist(),
        "rest_lengths_m": REST.copy(), "initial_positions_m": positions.tolist()}
    return c, positions, loading, tension


def direct_forces(positions, rest, loading):
    delta = np.diff(positions, axis=0)
    chords = np.linalg.norm(delta, axis=1)
    tension = np.maximum(chords-np.asarray(rest), 0)/loading["compliance"]
    axial = np.zeros_like(positions)
    segment = tension[:, None]*delta/chords[:, None]
    axial[:-1] += segment
    axial[1:] -= segment
    force = axial.copy()
    force[:, 2] -= loading["weight"]
    boundary = np.zeros_like(positions)
    boundary[[0, -1]] = -force[[0, -1]]
    return tension, force, boundary


def checksum(document):
    def canonical(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value
    payload = {key: value for key, value in document.items() if key != "checksum_sha256"}
    return hashlib.sha256(json.dumps(canonical(payload), sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture(scope="module")
def actual_initial():
    c, positions, loading, tension = configuration()
    return c, positions, loading, tension, simulate_lay({}, c)


def test_declared_mixed_intervals_and_bodies_match_public_initial_loads(actual_initial):
    c, positions, expected, tension, result = actual_initial
    f = result["frames"][0]
    assert f["nodes"] == pytest.approx(positions, abs=1e-9)
    assert f["node_material_m"] == pytest.approx(expected["coordinates"], abs=1e-12)
    assert f["segment_ea_n"] == pytest.approx(expected["ea"], rel=1e-12, abs=1e-9)
    assert f["segment_wet_weight_n_m"] == pytest.approx(expected["segment_wet"]/REST, abs=1e-12)
    assert f["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-11)
    assert f["node_dry_mass_kg"] == pytest.approx(expected["dry"], abs=1e-11)
    assert f["node_mass_kg"] == pytest.approx(expected["mass"], abs=1e-11)
    assert f["segment_tension_n"] == pytest.approx(tension, abs=1e-8)
    # A material boundary is strictly inside element 1. Its arithmetic mean
    # EA differs substantially from the series-compliance expectation.
    assert expected["ea"][1] == pytest.approx(2/(.75/14000+1.25/45000), rel=1e-14)
    assert abs(expected["ea"][1]-(.75*14000+1.25*45000)/2) > 5000
    assert min(expected["weight"]) < 0, "the buoyancy example exercises a signed nodal load"
    assert sum(f["node_wet_weight_n"]) == pytest.approx(sum(expected["segment_wet"])-15., abs=1e-11)
    assert c["initial_suspended_material_m"] == 37.


def test_v2_records_component_loads_and_body_virtual_work_not_only_net_totals(actual_initial):
    c, positions, expected, _, result = actual_initial
    proof = result["initialization"]
    assert proof["schema"] == PROVENANCE_V2
    parts = proof["material_loading"]
    assert parts["operator"] == OPERATOR and len(parts["declarations_sha256"]) == 64
    assert parts["segment_cable_wet_weight_n"] == pytest.approx(expected["segment_wet"], abs=1e-11)
    assert parts["segment_compliance_m_n"] == pytest.approx(expected["compliance"], rel=1e-12, abs=0)
    assert parts["node_cable_wet_weight_n"] == pytest.approx(expected["cable_wet"], abs=1e-11)
    assert parts["node_body_wet_weight_n"] == pytest.approx(expected["body_wet"], abs=1e-11)
    assert parts["signed_total_wet_weight_n"] == pytest.approx(sum(expected["weight"]), abs=1e-11)
    assert parts["absolute_load_scale_n"] == pytest.approx(expected["absolute_load_scale_n"], abs=1e-11)
    by_id = {body["id"]: body for body in parts["point_bodies"]}
    frames = {body["id"]: body for body in result["frames"][0]["inline_bodies"]}
    for body in c["inline_bodies"]:
        alpha = expected["alpha"][body["id"]]
        assert by_id[body["id"]]["node_fractions"] == pytest.approx(alpha, abs=1e-12)
        assert by_id[body["id"]]["deployed_fraction"] == pytest.approx(sum(alpha), abs=0)
        assert by_id[body["id"]]["material_m"] == body["material_m"]
        assert frames[body["id"]]["deployed_wet_weight_n"] == pytest.approx(sum(alpha)*body["wet_weight_n"], abs=1e-12)
        if sum(alpha):
            assert frames[body["id"]]["position"] == pytest.approx(alpha@positions, abs=1e-9)
        else:
            assert frames[body["id"]]["position"] is None
    # Virtual work of a non-node, negative-weight body is signed. An endpoint
    # perturbation of its element changes B*z_body by the claimed nodal force.
    direction = np.linspace(-.7, .6, 8)
    body = next(row for row in c["inline_bodies"] if row["id"] == "buoyant-inside")
    alpha = expected["alpha"][body["id"]]
    step = 1e-5
    energy_difference = body["wet_weight_n"]*(alpha@(positions[:, 2]+step*direction)-alpha@(positions[:, 2]-step*direction))/(2*step)
    assert energy_difference == pytest.approx(body["wet_weight_n"]*(alpha@direction), abs=1e-8)


def test_exact_seed_has_true_force_balance_and_endpoint_half_load_reactions(actual_initial):
    _, expected_positions, loading, expected_tension, result = actual_initial
    actual = np.asarray(result["frames"][0]["nodes"])
    tension, force, boundary = direct_forces(actual, REST, loading)
    assert tension == pytest.approx(expected_tension, abs=1e-8)
    assert np.max(np.linalg.norm(force[1:-1], axis=1)) < 1e-8
    snapshot = result["initialization"]["initial_snapshot"]
    assert snapshot["node_boundary_force_n"] == pytest.approx(boundary, abs=1e-8)
    # Independently prescribed H and bottom V fix reactions including both
    # endpoint half cable weights and loads placed at the fixed endpoints.
    vertical_top = math.fsum(loading["weight"][1:-1])+35.
    assert boundary[0] == pytest.approx([150., 0., vertical_top+loading["weight"][0]], abs=1e-8)
    assert boundary[-1] == pytest.approx([-150., 0., loading["weight"][-1]-35.], abs=1e-8)
    assert boundary.sum(axis=0) == pytest.approx([0., 0., sum(loading["weight"])], abs=1e-8)
    assert actual[-1] == pytest.approx(expected_positions[-1], abs=1e-12)
    assert result["frames"][0]["touchdown"] is None and result["frames"][0]["bottom_tension_n"] is None
    assert result["frames"][0]["touchdown_detected"] is False


def test_bad_seed_is_actually_optimized_using_mixed_compliance_and_signed_loads():
    c, exact, loading, _ = configuration()
    seed = exact.copy()
    perturbation = np.sin(np.linspace(.2, 2.7, 6))
    seed[1:-1, 0] += .2*perturbation
    seed[1:-1, 2] += .12*perturbation
    assert np.max(np.linalg.norm(direct_forces(seed, REST, loading)[1][1:-1], axis=1)) > 300
    c["initial_equilibrium"]["initial_positions_m"] = seed.tolist()
    prepared = resolve_initial_equilibrium({}, c)
    proof = prepared["provenance"]
    actual = np.asarray(proof["initial_snapshot"]["positions"])
    # A real optimizer must leave the non-equilibrium guess, recover the
    # independently constructed shape and satisfy the declared .01 N limit.
    assert proof["solver"]["iterations"] > 0
    assert np.max(np.linalg.norm(actual-exact, axis=1)) < 1e-5
    assert np.max(np.linalg.norm(direct_forces(actual, REST, loading)[1][1:-1], axis=1)) < .01


def test_signed_point_loads_and_contact_reaction_are_not_double_loaded():
    c, _, loading, _ = configuration(duration_s=.1)
    vertical = np.zeros(7)
    vertical[3:5] = [4., -4.]
    for i in range(2, -1, -1):
        vertical[i] = vertical[i+1]+loading["weight"][i+1]
    for i in range(5, 7):
        vertical[i] = vertical[i-1]-loading["weight"][i]
    tension = np.hypot(150., vertical)
    direction = np.column_stack((150./tension, np.zeros(7), vertical/tension))
    vectors = (np.asarray(REST)+tension*loading["compliance"])[:, None]*direction
    vessel = np.array([3., -4., -2.])
    positions = np.vstack((vessel, vessel-np.cumsum(vectors, axis=0)))
    bed = positions[4, 2]
    c["seabed_grid"]["z_m"] = [[float(bed)]*3 for _ in range(3)]
    c["initial_equilibrium"]["anchor_position_m"] = positions[-1].tolist()
    c["initial_equilibrium"]["initial_positions_m"] = positions.tolist()
    result = simulate_lay({}, c)
    snapshot = result["initialization"]["initial_snapshot"]
    # The node at the bottom turns from V=4 to V=-4. Its true bed reaction is
    # weight - 8 N; buoyant node 6 remains off-bed. The fixed anchor is off-bed.
    normal = np.zeros(8); normal[4] = loading["weight"][4]-8.
    assert normal[4] == 6.5 and loading["weight"][6] < 0
    assert snapshot["node_contact_normal_force_n"] == pytest.approx(normal, abs=1e-8)
    force = direct_forces(np.asarray(snapshot["positions"]), REST, loading)[1]
    force[4, 2] += normal[4]
    assert np.max(np.linalg.norm(force[1:-1], axis=1)) < 1e-8
    assert positions[-1, 2]-bed > .5
    assert result["frames"][0]["touchdown_detected"] is True
    assert result["frames"][0]["touchdown"] == pytest.approx(positions[4], abs=1e-9)
    assert np.max(np.linalg.norm(np.asarray(result["frames"][-1]["nodes"])-positions, axis=1)) < 1e-7


def test_real_mixed_material_solve_keeps_bilinear_bed_normals_and_all_chord_clearance():
    c, _, _, _ = configuration(nodes=18, duration_s=.02)
    axis = [-100., 0., 100.]
    c["seabed_grid"]["z_m"] = [[-40+.1*x+.05*y+.0003*x*y for x in axis] for y in axis]
    c["material_segments"][-1]["end_m"] = 150.
    c["initial_equilibrium"] = {"schema": "oceanroute.dynamic.initial-equilibrium.v1",
        "vessel_position_m": [0., 0., 0.], "anchor_position_m": [-60., -10., -46.32],
        "natural_length_m": 80.}
    result = simulate_lay({}, c)
    snapshot = result["initialization"]["initial_snapshot"]
    p = np.asarray(snapshot["positions"])
    rest = snapshot["rest_lengths_m"]
    loading = declared_loading(c, rest)
    floor = -40+.1*p[:, 0]+.05*p[:, 1]+.0003*p[:, 0]*p[:, 1]
    normal = np.column_stack((-.1-.0003*p[:, 1], -.05-.0003*p[:, 0], np.ones(len(p))))
    normal /= np.linalg.norm(normal, axis=1)[:, None]
    force = direct_forces(p, rest, loading)[1]
    support = np.maximum(-np.sum(force*normal, axis=1), 0)*(p[:, 2]-floor <= 1e-8)
    support[[0, -1]] = 0
    assert np.count_nonzero(support) >= 2
    assert snapshot["node_contact_normal_force_n"] == pytest.approx(support, abs=1e-8)
    assert result["frames"][0]["node_seabed_normal"] == pytest.approx(normal, abs=1e-12)
    assert np.max(np.linalg.norm((force+support[:, None]*normal)[1:-1], axis=1)) < .01
    boundary = np.asarray(snapshot["node_boundary_force_n"])
    assert boundary[[0, -1]] == pytest.approx(-force[[0, -1]], abs=1e-8)
    assert np.linalg.norm(boundary.sum(axis=0)+(support[:, None]*normal).sum(axis=0)-
                          np.array([0., 0., sum(loading["weight"])])) < .16
    # On this genuine xy-bilinear surface the gap along a straight element is
    # quadratic. Check its exact interior extremum, not a node-only or sampled
    # approximation, without invoking the production bed/clearance helpers.
    for a, b in zip(p[:-1], p[1:]):
        dx, dy, dz = b-a
        qa = -.0003*dx*dy
        qb = dz-.1*dx-.05*dy-.0003*(a[0]*dy+a[1]*dx)
        qc = a[2]+40-.1*a[0]-.05*a[1]-.0003*a[0]*a[1]
        values = [qc, qa+qb+qc]
        if qa > 0 and 0 < -qb/(2*qa) < 1:
            u = -qb/(2*qa)
            values.append(qa*u*u+qb*u+qc)
        assert min(values) >= -1e-8
    assert sum(rest) == pytest.approx(80., abs=1e-12)
    assert result["frames"][0]["paid_out_m"] == 0


@pytest.mark.parametrize("internal_dt", [.004, .002])
def test_actual_heterogeneous_prestress_is_a_fixed_point_without_locking(internal_dt):
    c, expected, loading, _ = configuration(duration_s=.1, internal_dt_s=internal_dt)
    result = simulate_lay({}, c)
    last = result["frames"][-1]
    assert np.max(np.linalg.norm(np.asarray(last["nodes"])-expected, axis=1)) < 1e-7
    assert np.max(np.linalg.norm(np.asarray(last["node_velocity_m_s"]), axis=1)) < 1e-6
    assert np.max(np.linalg.norm(direct_forces(np.asarray(last["nodes"]), REST, loading)[1][1:-1], axis=1)) < 1e-7
    assert len(last["nodes"]) == 8 and last["ship"][2] == -2.
    assert last["paid_out_m"] == 0 and last["material_length_m"] == 27.
    assert result["checkpoint"]["schema_version"] == 3
    assert result["model"] == "material-lumped-mass-xpbd-cable-lay-v4"


def test_actual_motion_feed_and_future_point_deployment_do_not_repay_initial_stock():
    c, original, _, _ = configuration(ship_speed_m_s=.2, payout_m_s=.25, duration_s=.08)
    result = simulate_lay({}, c)
    last = result["frames"][-1]
    state = result["checkpoint"]["state"]
    assert last["ship"] == pytest.approx([3.016, -4., -2.], abs=1e-12)
    assert last["nodes"][-1] == pytest.approx(original[-1], abs=1e-12)
    assert np.max(np.linalg.norm(np.asarray(last["nodes"])[1:-1]-original[1:-1], axis=1)) > 1e-7
    assert last["paid_out_m"] == pytest.approx(.02, abs=1e-13)
    assert sum(state["rest_lengths_m"]) == pytest.approx(27.02, abs=1e-12)
    assert last["node_material_m"][0] == pytest.approx(64.02, abs=1e-12)
    assert last["node_material_m"][-1] == 37.
    expected = declared_loading(c, state["rest_lengths_m"])
    assert last["node_wet_weight_n"] == pytest.approx(expected["weight"], abs=1e-10)
    assert last["node_mass_kg"] == pytest.approx(expected["mass"], abs=1e-10)
    body = next(row for row in last["inline_bodies"] if row["id"] == "future-point")
    assert body["deployed_fraction"] == 1 and body["deployed_wet_weight_n"] == -2.


def test_real_rotation_covariance_preserves_mixed_material_and_point_load_equilibrium(actual_initial):
    _, positions, loading, _, base = actual_initial
    c, rotated, _, _ = configuration(rotation=math.pi/2)
    result = simulate_lay({}, c)
    assert result["frames"][0]["nodes"] == pytest.approx(rotated, abs=1e-9)
    expected = positions.copy();expected[:, 0], expected[:, 1] = -positions[:, 1], positions[:, 0]
    assert result["frames"][0]["nodes"] == pytest.approx(expected, abs=1e-9)
    assert result["frames"][0]["segment_tension_n"] == pytest.approx(base["frames"][0]["segment_tension_n"], abs=1e-8)
    assert result["frames"][0]["node_wet_weight_n"] == pytest.approx(loading["weight"], abs=1e-11)


def test_opposing_nearby_body_loads_do_not_hide_absolute_force_scale():
    delta = 2**-18
    bodies = [{"id": "positive", "material_m": 47.+delta, "mass_kg": 20000., "wet_weight_n": 100000., "length_m": 0.},
              {"id": "negative", "material_m": 47.-delta, "mass_kg": 1., "wet_weight_n": -100000., "length_m": 0.}]
    c, _, loading, _ = configuration(bodies=bodies)
    prepared = resolve_initial_equilibrium({}, c)
    parts = prepared["provenance"]["material_loading"]
    assert parts["absolute_load_scale_n"] == pytest.approx(loading["absolute_load_scale_n"], abs=1e-8)
    assert parts["absolute_load_scale_n"] > 200000 and abs(sum(loading["weight"])) < 200
    assert prepared["provenance"]["verification"]["force_tolerance_n"] == pytest.approx(
        loading["absolute_load_scale_n"]*1e-7, abs=1e-12)


def test_json_checkpoint_restores_mixed_loading_without_reoptimizing_or_repaying():
    c, _, _, _ = configuration(payout_m_s=.25, duration_s=.08)
    full = simulate_lay({}, c)
    first = simulate_lay({}, {**c, "duration_s": .04})
    saved = json.loads(json.dumps(first["checkpoint"], allow_nan=False))
    read_checkpoint(saved)
    restored = simulate_lay({}, {"resume_state": saved, "duration_s": .04})
    for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_wet_weight_n", "segment_ea_n"):
        assert restored["checkpoint"]["state"][key] == pytest.approx(np.asarray(full["checkpoint"]["state"][key]), abs=1e-9)
    assert restored["initialization"] == first["initialization"]
    assert restored["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False
    assert restored["solver"]["initialization_work"]["estimated_work_units_this_run"] == 0
    assert restored["solver"]["initialization_work"]["checkpoint_proof_verification_work_this_run"] > 0
    assert restored["checkpoint"]["state"]["paid_out_m"] == pytest.approx(.02, abs=1e-13)


@pytest.mark.parametrize("mutation", ["wet_declaration", "body_station", "body_mass", "fractions", "compliance", "component_load", "force_evidence", "proof_downgrade", "current_load"])
def test_recomputed_checksum_cannot_authorize_wrong_loading_or_downgrade(actual_initial, mutation):
    cp = deepcopy(actual_initial[-1]["checkpoint"])
    proof = cp["state"]["initialization_provenance"]
    if mutation == "wet_declaration":
        cp["config"]["inline_bodies"][1]["wet_weight_n"] -= 1.
    elif mutation == "body_station":
        cp["config"]["inline_bodies"][1]["material_m"] += .1
    elif mutation == "body_mass":
        cp["config"]["inline_bodies"][1]["mass_kg"] += 1.
    elif mutation == "fractions":
        body = next(row for row in proof["material_loading"]["point_bodies"] if row["id"] == "inside-upper")
        body["node_fractions"][0] += .1;body["node_fractions"][1] -= .1
    elif mutation == "compliance":
        proof["material_loading"]["segment_compliance_m_n"][1] *= 1.1
    elif mutation == "component_load":
        proof["material_loading"]["node_body_wet_weight_n"][3] += 1.
    elif mutation == "force_evidence":
        proof["initial_snapshot"]["node_boundary_force_n"][0][2] += 1.
    elif mutation == "proof_downgrade":
        proof["schema"] = "oceanroute.dynamic.initial-equilibrium.provenance.v1"
        proof.pop("material_loading")
    else:
        cp["state"]["node_wet_weight_n"][2] += 1.
    cp["checksum_sha256"] = checksum(cp)
    with pytest.raises(ValueError):
        read_checkpoint(cp)


@pytest.mark.parametrize("location", ["current", "original_snapshot"])
def test_large_origin_material_station_evidence_uses_absolute_not_origin_scaled_tolerance(location):
    c, _, _, _ = configuration()
    shift = 1e8-ORIGIN
    c["initial_suspended_material_m"] = 1e8
    for i, row in enumerate(c["material_segments"]):
        if i:
            row["start_m"] += shift
        row["end_m"] += shift
    for body in c["inline_bodies"]:
        body["material_m"] += shift
    checkpoint = deepcopy(simulate_lay({}, c)["checkpoint"])
    assert checkpoint["time_s"] > 0
    state = checkpoint["state"] if location == "current" else checkpoint["state"]["initialization_provenance"]["initial_snapshot"]
    before = state["node_material_m"][2]
    state["node_material_m"][2] += .001
    assert state["node_material_m"][2]-before > 60000*math.ulp(before)
    checkpoint["checksum_sha256"] = checksum(checkpoint)
    with pytest.raises(ValueError, match="(?i)material|manufacturing"):
        read_checkpoint(checkpoint)


@pytest.mark.parametrize("reason", ["rod", "ei", "flow", "wave", "budget", "accepted", "material_gap"])
def test_unsupported_initial_physics_or_false_authority_is_rejected(reason):
    c, _, _, _ = configuration()
    if reason == "rod":
        c["inline_bodies"][1]["length_m"] = .5
    elif reason == "ei":
        c["material_segments"][1]["ei_n_m2"] = 1.
    elif reason == "flow":
        c["current_x_m_s"] = .1
    elif reason == "wave":
        c["wave_kinematics"] = {"depth_m": 100., "time_origin_s": 0., "spatial_origin_xy_m": [0., 0.],
            "components": [{"frequency_hz": .25, "amplitude_m": .1, "phase_deg": 0., "direction_deg": 90.}]}
    elif reason == "budget":
        c["initial_equilibrium"]["solver"] = {"max_work_units": 1}
    elif reason == "accepted":
        c["initial_equilibrium"]["accepted"] = True
    else:
        c["material_segments"][1]["start_m"] += .1
    expected_error = {"rod": "(?i)finite|rod|length", "ei": "(?i)ei|bend", "flow": "(?i)current|flow",
                      "wave": "wave", "budget": "max_work_units|computation|budget",
                      "accepted": "unsupported inputs|accepted", "material_gap": "contiguous|cover|gap"}[reason]
    with pytest.raises(ValueError, match=expected_error):
        simulate_lay({}, c)


@pytest.mark.parametrize("cross_boundary", [False, True])
def test_shared_material_integrals_survive_large_history_and_tiny_exact_binary_segments(cross_boundary):
    origin = float(2**26)
    c = {"initial_suspended_material_m": origin, "wet_weight_n_m": 4., "diameter_m": .02,
         "ea_n": 10000., "ei_n_m2": 0., "mass_kg_m": 1., "water_density_kg_m3": 1025.,
         "added_mass_coefficient": .7,
         "material_segments": [{"start_m": 0., "end_m": origin, "wet_weight_n_m": 20000.,
             "mass_kg_m": 3000., "diameter_m": .02, "ea_n": 100., "ei_n_m2": 0.}]}
    if cross_boundary:
        c["material_segments"].append({"start_m": origin, "end_m": origin+1/2048,
            "wet_weight_n_m": 2., "mass_kg_m": .5, "diameter_m": .01, "ea_n": 100., "ei_n_m2": 0.})
    c["material_segments"].append({"start_m": origin+1/2048 if cross_boundary else origin,
        "end_m": origin+1., "wet_weight_n_m": 1e-6, "mass_kg_m": .01,
        "diameter_m": .0001, "ea_n": 1e12, "ei_n_m2": 0.})
    rest = [1/1024]*5
    expected = declared_loading(c, rest)
    subject = _MaterialModel(c, _environment(c), 10000., 0., 1., .7).loads(np.asarray(rest))
    assert np.isfinite(subject["ea"]).all() and np.all(subject["ea"] > 0)
    assert subject["ea"] == pytest.approx(expected["ea"], rel=1e-12, abs=0)
    assert subject["weight"] == pytest.approx(expected["weight"], rel=1e-12, abs=0)
    assert subject["dry_mass"] == pytest.approx(expected["dry"], rel=1e-12, abs=0)
    assert subject["mass"] == pytest.approx(expected["mass"], rel=1e-12, abs=0)
    assert sum(subject["dry_mass"]) == pytest.approx(sum(expected["dry"]), rel=1e-12, abs=0)
    if cross_boundary:
        assert subject["ea"][-1] == pytest.approx(2/(1/100+1/1e12), rel=1e-12)
    else:
        assert sum(subject["weight"]) == pytest.approx(5/1024*1e-6, rel=1e-12, abs=0)


@pytest.mark.parametrize("length", [.001, .0001])
def test_large_origin_relative_integrals_and_point_shares_do_not_round_each_node_kp(length):
    origin = 1e8
    # The source's cut and body station are their exact declared float values.
    # No extra precision is imputed to the decimal text. The prescribed rest
    # lengths must not acquire an unrelated error from adding origin to each
    # node before a short local material overlap or point fraction is computed.
    cut = origin+2.6*length
    c = {"initial_suspended_material_m": origin, "wet_weight_n_m": 4., "diameter_m": .02,
         "ea_n": 10000., "ei_n_m2": 0., "mass_kg_m": 1., "water_density_kg_m3": 1025.,
         "added_mass_coefficient": .7,
         "material_segments": [
             {"start_m": 0., "end_m": cut, "wet_weight_n_m": 2., "mass_kg_m": .5,
              "diameter_m": .01, "ea_n": 100., "ei_n_m2": 0.},
             {"start_m": cut, "end_m": origin+1., "wet_weight_n_m": 1e-6,
              "mass_kg_m": .01, "diameter_m": .0001, "ea_n": 1e12, "ei_n_m2": 0.}],
         "inline_bodies": [{"id": "relative-point", "material_m": cut, "length_m": 0.,
                            "mass_kg": .4, "wet_weight_n": -.2}]}
    rest = [length]*5
    expected = declared_loading(c, rest)
    subject = _MaterialModel(c, _environment(c), 10000., 0., 1., .7).loads(np.asarray(rest))
    assert subject["segment_cable_wet_weight_n"] == pytest.approx(expected["segment_wet"], rel=1e-12, abs=0)
    assert subject["segment_compliance_m_n"] == pytest.approx(expected["compliance"], rel=1e-12, abs=0)
    assert subject["ea"] == pytest.approx(expected["ea"], rel=1e-12, abs=0)
    assert subject["body_weights"][0] == pytest.approx(expected["alpha"]["relative-point"], rel=1e-12, abs=1e-14)
    assert subject["dry_mass"] == pytest.approx(expected["dry"], rel=1e-12, abs=1e-14)
    assert subject["mass"] == pytest.approx(expected["mass"], rel=1e-12, abs=1e-14)


@pytest.mark.parametrize("label_length,duration,save_all", [(1010000, .02, False), (80000, 2., True)])
def test_repeated_point_metadata_respects_standalone_and_batch_preflight(label_length, duration, save_all, monkeypatch):
    c, _, _, _ = configuration(duration_s=duration, save_checkpoints=save_all)
    c["inline_bodies"][0]["id"] = "x"*label_length
    import oceanroute.initial_equilibrium as initializer
    original = initializer.resolve_initial_equilibrium
    optimized = []
    def observe_real_solve(*args, **kwargs):
        # This is an invocation observer only. If reached it delegates to the
        # actual static/mapping solve, never returning a fabricated candidate.
        optimized.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(initializer, "resolve_initial_equilibrium", observe_real_solve)
    # These inputs otherwise satisfy the actual material/geometry/12M dynamic
    # limits. One repeated ID exceeds the 2 MB checkpoint; the other repeats
    # through 101 actual checkpoints and exceeded the 16 MB batch before this
    # regression. An explicit body-ID input limit is also safe early rejection.
    with pytest.raises(ValueError, match="(?i)checkpoint JSON volume|body.*id.*limit|id.*length"):
        simulate_lay({}, c)
    assert not optimized, "capacity rejection must precede the real static optimizer"


def test_repeated_frame_point_ids_respect_complete_response_preflight(monkeypatch):
    c, _, _, _ = configuration(duration_s=2.)
    c["inline_bodies"][0]["id"] = "x"*800000
    import oceanroute.initial_equilibrium as initializer
    original = initializer.resolve_initial_equilibrium
    optimized = []
    def observe_real_solve(*args, **kwargs):
        optimized.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(initializer, "resolve_initial_equilibrium", observe_real_solve)
    # The actual prior run returned 85,220,210 B from 101 frames despite valid
    # 1.61 MB standalone/selected checkpoints and 11,770,126 dynamic work units.
    # No physics model or finite-ID truncation is needed to enforce 64 MB.
    with pytest.raises(ValueError, match="(?i)response.*volume|output.*volume|result.*volume"):
        simulate_lay({}, c)
    assert not optimized, "total response capacity must be checked before the real solve"


def completed(client, identifier):
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        response = client.get(f"/api/voyage/jobs/{identifier}")
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] not in {"queued", "running", "cancelling"}:
            assert state["status"] == "completed", state
            return state
        time.sleep(.01)
    pytest.fail("real heterogeneous initial-state job did not complete")


def test_true_http_job_owner_close_reopen_resume_matches_whole_run(tmp_path):
    c, _, _, _ = configuration(payout_m_s=.25, duration_s=.08)
    c.pop("checkpoint_times_s")  # the voyage owns physical-checkpoint timing
    policy = {"simulation": c, "duration_s": .08, "chunk_duration_s": .02,
              "max_total_work_units": 120000000, "adaptive_mesh": {"enabled": False}}
    whole = run_voyage({}, policy)
    assert whole["status"] == "completed"
    database = tmp_path/"heterogeneous.sqlite3"
    with TestClient(create_app(ProjectStore(database))) as client:
        response = client.post("/api/simulation/dynamic", json={"project": {}, "config": c})
        assert response.status_code == 200, response.text
        assert response.json()["initialization"]["schema"] == PROVENANCE_V2
        response = client.post("/api/voyage/jobs", json={"project": {}, "config": {**policy, "duration_s": .04}})
        assert response.status_code == 200, response.text
        parent = response.json()["id"]
        completed(client, parent)
        saved = client.get(f"/api/voyage/jobs/{parent}/checkpoint").json()
        read_voyage_checkpoint(saved)
    with TestClient(create_app(ProjectStore(database))) as client:
        assert client.get(f"/api/voyage/jobs/{parent}/checkpoint").json() == saved
        response = client.post(f"/api/voyage/jobs/{parent}/resume", json={"duration_s": .04})
        assert response.status_code == 200, response.text
        child = response.json()["id"]
        assert completed(client, child)["parent_job_id"] == parent
        actual = client.get(f"/api/voyage/jobs/{child}/result").json()
        for key in ("positions", "velocities", "rest_lengths_m", "node_material_m", "node_mass_kg", "node_wet_weight_n", "segment_ea_n"):
            assert actual["checkpoint"]["physical_checkpoint"]["state"][key] == pytest.approx(np.asarray(whole["checkpoint"]["physical_checkpoint"]["state"][key]), abs=1e-9)
        assert actual["checkpoint"]["physical_checkpoint"]["state"]["initialization_provenance"] == saved["physical_checkpoint"]["state"]["initialization_provenance"]
        assert actual["summary"]["paid_out_m"] == pytest.approx(.02, abs=1e-13)
        assert all(row["solver"]["initialization_work"]["static_optimizer_run_this_call"] is False for row in actual["chunks"])
        assert actual["frames"][0]["time_s"] == .04 and actual["frames"][-1]["time_s"] == .08
        read_voyage_checkpoint(json.loads(json.dumps(actual["checkpoint"], allow_nan=False)))
